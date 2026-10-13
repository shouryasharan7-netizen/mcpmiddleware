#!/usr/bin/env python3
"""MCP gateway with OIDC JWT validation, Redis sessions, and streaming proxy routes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web, WSMsgType
import jwt
from jwt import PyJWKClient
from redis.asyncio import Redis
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from adapters import call_grpc, call_rest, discover_grpc, openapi_operations
from security import redact_tree
from pipeline import get_pipeline, RequestContext, ResponseContext, ErrorContext
from guardrails import GuardrailsMiddleware
from telemetry import TelemetryMiddleware

DB_PATH = os.getenv("MCP_GATEWAY_DB", "gateway.sqlite3")
AUDIT_PATH = os.getenv("MCP_GATEWAY_AUDIT", "audit.jsonl")
MAX_BODY = int(os.getenv("MCP_GATEWAY_MAX_BODY", "1048576"))
SESSION_TTL = int(os.getenv("MCP_SESSION_TTL_SECONDS", "3600"))
KEEPALIVE = float(os.getenv("MCP_SSE_KEEPALIVE_SECONDS", "15"))
INJECTION = re.compile(r"(?i)(ignore\s+(all\s+)?(previous|prior)\s+instructions|\b(?:rm\s+-rf|sudo\s+|curl\s+[^\n]*\|\s*(?:sh|bash)|__import__\s*\(|os\.system\s*\())")


def database():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""CREATE TABLE IF NOT EXISTS tools (
      name TEXT PRIMARY KEY, endpoint TEXT NOT NULL, definition TEXT NOT NULL,
      roles TEXT NOT NULL DEFAULT '["mcp.engineer"]', updated INTEGER NOT NULL)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tools)")}
    if "transport" not in columns:
        conn.execute("ALTER TABLE tools ADD COLUMN transport TEXT NOT NULL DEFAULT 'streamable-http'")
    if "read_roles" not in columns:
        conn.execute("ALTER TABLE tools ADD COLUMN read_roles TEXT")
        conn.execute("UPDATE tools SET read_roles=roles WHERE read_roles IS NULL")
    if "write_roles" not in columns:
        conn.execute("ALTER TABLE tools ADD COLUMN write_roles TEXT")
        conn.execute("UPDATE tools SET write_roles=roles WHERE write_roles IS NULL")
    for column in ("auth_env", "federation", "source_tool", "adapter_config"):
        if column not in columns: conn.execute(f"ALTER TABLE tools ADD COLUMN {column} TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS federations (
      name TEXT PRIMARY KEY, tools_url TEXT NOT NULL, mcp_url TEXT NOT NULL,
      token_env TEXT NOT NULL, read_roles TEXT NOT NULL, write_roles TEXT NOT NULL,
      updated INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_events (
      id INTEGER PRIMARY KEY, timestamp INTEGER NOT NULL, user TEXT NOT NULL,
      execution_id TEXT NOT NULL UNIQUE, session TEXT NOT NULL, model TEXT NOT NULL, method TEXT NOT NULL,
      tool TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
      execution_sha256 TEXT NOT NULL, verdict TEXT NOT NULL, detail TEXT NOT NULL)""")
    audit_columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_events)")}
    if "payload_json" not in audit_columns:
        conn.execute("ALTER TABLE audit_events ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'")
    if "execution_id" not in audit_columns:
        conn.execute("ALTER TABLE audit_events ADD COLUMN execution_id TEXT")
        conn.execute("UPDATE audit_events SET execution_id='legacy-' || id WHERE execution_id IS NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS audit_execution_id_idx ON audit_events(execution_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_outbox (
      execution_id TEXT PRIMARY KEY, event_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0)""")
    return conn


def configure_telemetry():
    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "secure-mcp-gateway")})
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        trace_provider = TracerProvider(resource=resource)
        trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(trace_provider)
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=10000)
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
    return trace.get_tracer("secure_mcp_gateway"), metrics.get_meter("secure_mcp_gateway")


TRACER, METER = configure_telemetry()
REQUESTS = METER.create_counter("mcp.gateway.requests", description="Gateway requests")
LATENCY = METER.create_histogram("mcp.gateway.request.duration", unit="ms")
JWKS = PyJWKClient(os.environ.get("MCP_OIDC_JWKS_URI", "https://invalid.local/jwks"), cache_jwk_set=True)


def bearer_identity(request):
    token = request.headers.get("Authorization", "")
    if not token.startswith("Bearer "):
        raise web.HTTPUnauthorized(text="bearer token required")
    issuer, audience = os.getenv("MCP_OIDC_ISSUER"), os.getenv("MCP_OIDC_AUDIENCE")
    if not issuer or not audience or "MCP_OIDC_JWKS_URI" not in os.environ:
        raise web.HTTPServiceUnavailable(text="OIDC issuer, audience, and JWKS URI must be configured")
    try:
        key = JWKS.get_signing_key_from_jwt(token[7:])
        claims = jwt.decode(token[7:], key.key, algorithms=["RS256", "ES256"], issuer=issuer,
                            audience=audience, options={"require": ["exp", "sub", "iss", "aud"]})
    except Exception as exc:
        raise web.HTTPUnauthorized(text="invalid access token") from exc
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise web.HTTPUnauthorized(text="token subject required")
    values = claims.get("roles", claims.get("groups", []))
    if isinstance(values, str): values = [values]
    if not isinstance(values, list): values = []
    role_map = json.loads(os.getenv("MCP_ROLE_MAP", "{}"))
    roles = set(x for x in values if isinstance(x, str))
    for role, groups in role_map.items():
        if isinstance(groups, str): groups = [groups]
        if set(groups) & roles: roles.add(role)
    realm = claims.get("realm_access", {}).get("roles", [])
    if isinstance(realm, list): roles.update(x for x in realm if isinstance(x, str))
    return {"sub": subject, "roles": sorted(roles),
            "session": str(claims.get("sid", claims.get("jti", subject))),
            "model": str(claims.get("model", "unspecified"))}


class SessionStore:
    def __init__(self):
        redis_url = os.getenv("MCP_REDIS_URL")
        allow_local = os.getenv("MCP_ALLOW_IN_MEMORY_SESSIONS", "").lower() == "true"
        if not redis_url and not allow_local:
            raise RuntimeError("MCP_REDIS_URL is required; set MCP_ALLOW_IN_MEMORY_SESSIONS=true only for local development")
        self.redis = Redis.from_url(redis_url, decode_responses=True) if redis_url else None
        self.local = {}

    async def get(self, key):
        if self.redis: return await self.redis.get("mcp:session:" + key)
        value = self.local.get(key)
        if value and value[0] > time.time(): return value[1]
        self.local.pop(key, None)
        return None

    async def set(self, key, value):
        if self.redis: await self.redis.set("mcp:session:" + key, value, ex=SESSION_TTL)
        else: self.local[key] = (time.time() + SESSION_TTL, value)

    async def delete(self, key):
        if self.redis: await self.redis.delete("mcp:session:" + key)
        self.local.pop(key, None)


async def audit(app, identity, method, tool, payload, verdict, detail="", redactions=None):
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    event = {"timestamp": int(time.time()), "execution_id": secrets.token_hex(16), "user": identity.get("sub", "unknown"),
             "session": identity.get("session", "unknown"), "model": identity.get("model", "unspecified"),
             "method": method, "tool": tool, "payload_sha256": payload_hash,
             "verdict": verdict, "detail": detail, "redactions": redactions or {}}
    event["execution_sha256"] = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with TRACER.start_as_current_span("mcp.audit.fingerprint") as span:
        for key, value in (("mcp.audit.execution_id", event["execution_id"]), ("mcp.audit.payload_sha256", payload_hash),
                           ("mcp.audit.execution_sha256", event["execution_sha256"]), ("mcp.audit.verdict", verdict),
                           ("mcp.tool.name", tool), ("enduser.id", event["user"]), ("mcp.session.id", event["session"]),
                           ("mcp.model.version", event["model"])):
            span.set_attribute(key, value)
        span.add_event("mcp.audit.verdict", {"execution_id": event["execution_id"], "payload_sha256": payload_hash,
                                              "execution_sha256": event["execution_sha256"], "verdict": verdict})
    conn = database()
    try:
        conn.execute("INSERT INTO audit_events(timestamp,user,execution_id,session,model,method,tool,payload_json,payload_sha256,execution_sha256,verdict,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (event["timestamp"], event["user"], event["execution_id"], event["session"], event["model"], method, tool,
                      json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False), payload_hash,
                      event["execution_sha256"], verdict, detail))
        if os.getenv("MCP_AUDIT_WEBHOOK_URL") and os.getenv("MCP_SPLUNK_HEC_TOKEN"):
            conn.execute("INSERT INTO audit_outbox(execution_id,event_json,next_attempt) VALUES(?,?,?)",
                         (event["execution_id"], json.dumps(event, separators=(",", ":")), int(time.time())))
        conn.commit()
    finally: conn.close()
    line = (json.dumps(event, separators=(",", ":")) + "\n").encode()
    fd = os.open(AUDIT_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try: os.write(fd, line)
    finally: os.close(fd)


async def audit_delivery_worker(app):
    url, token = os.getenv("MCP_AUDIT_WEBHOOK_URL"), os.getenv("MCP_SPLUNK_HEC_TOKEN")
    if not url or not token: return
    while True:
        conn = database()
        try:
            pending = conn.execute("SELECT execution_id,event_json,attempts FROM audit_outbox WHERE delivered=0 AND next_attempt<=? ORDER BY next_attempt LIMIT 50", (int(time.time()),)).fetchall()
        finally: conn.close()
        for row in pending:
            try:
                async with app["http"].post(url, json={"event": json.loads(row["event_json"])},
                                            headers={"Authorization": "Splunk " + token}, timeout=5) as response:
                    if response.status < 300:
                        conn = database()
                        try: conn.execute("UPDATE audit_outbox SET delivered=1 WHERE execution_id=?", (row["execution_id"],)); conn.commit()
                        finally: conn.close()
                        continue
                    raise RuntimeError(f"HEC returned HTTP {response.status}")
            except Exception as exc:
                attempts = row["attempts"] + 1
                retry_at = int(time.time()) + min(3600, 2 ** min(attempts, 10))
                conn = database()
                try: conn.execute("UPDATE audit_outbox SET attempts=?,next_attempt=? WHERE execution_id=?", (attempts, retry_at, row["execution_id"])); conn.commit()
                finally: conn.close()
                app["logger"].warning("audit delivery failed for %s: %s", row["execution_id"], exc)
        await asyncio.sleep(1)


def tool_rows():
    conn = database()
    try: return conn.execute("SELECT * FROM tools ORDER BY name").fetchall()
    finally: conn.close()


def jsonrpc_error(request, code, message, status=200):
    return web.json_response({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": code, "message": message}}, status=status)


async def tools_list(request, message, identity):
    session_id = request.headers.get("Mcp-Session-Id")
    session_data = await request.app["sessions"].get(session_id) if session_id else None
    if session_id and not session_data: return jsonrpc_error(message, -32001, "unknown or expired MCP session", 404)
    session_record = json.loads(session_data) if session_data else None
    if session_record and session_record.get("user") != identity["sub"]: return web.json_response({"error": "session owner mismatch"}, status=403)
    session_tool_names = set(session_record.get("routes", {})) if session_record else None
    tools = [json.loads(row["definition"]) for row in tool_rows()
             if row["transport"] in ("streamable-http", "rest", "grpc") and (session_tool_names is None or row["name"] in session_tool_names)
             and set(identity["roles"]) & set(json.loads(row["read_roles"]))]
    response = web.json_response({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": tools}})
    if request.headers.get("Mcp-Session-Id"): response.headers["Mcp-Session-Id"] = request.headers["Mcp-Session-Id"]
    return response


async def relay(request, upstream_url, payload, downstream_session=None, auth_env=None):
    headers = {"Content-Type": request.headers.get("Content-Type", "application/json"),
               "Accept": request.headers.get("Accept", "application/json, text/event-stream")}
    for name in ("MCP-Protocol-Version", "Last-Event-ID"):
        if name in request.headers: headers[name] = request.headers[name]
    if downstream_session: headers["Mcp-Session-Id"] = downstream_session
    if auth_env:
        credential = os.getenv(auth_env)
        if not credential: raise web.HTTPServiceUnavailable(text=f"federation credential environment variable {auth_env} is not set")
        headers["Authorization"] = "Bearer " + credential
    data = json.dumps(payload).encode() if payload is not None else None
    upstream = await request.app["http"].request(request.method, upstream_url, data=data, headers=headers, allow_redirects=False)
    return upstream


async def _redact_sse_frame(frame, request, dlp_context):
    if not dlp_context: return frame
    lines = frame.splitlines()
    data_indexes = [i for i, line in enumerate(lines) if line.startswith(b"data:")]
    if not data_indexes: return frame
    payload = "\n".join(lines[i][5:].lstrip().decode("utf-8", errors="replace") for i in data_indexes)
    counts = {}
    try:
        value = json.loads(payload)
        clean = redact_tree(value, counts)
        serialized = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        from security import redact_text
        serialized = redact_text(payload, counts)
    if counts:
        await audit(request.app, dlp_context["identity"], dlp_context["method"] + ".output", dlp_context["tool"],
                    serialized, "redacted", "tool output scrubbed", counts)
    encoded_lines = serialized.splitlines() or [""]
    rewritten = []
    for index, line in enumerate(lines):
        if index == data_indexes[0]: rewritten.extend(("data: " + item).encode() for item in encoded_lines)
        if index not in data_indexes: rewritten.append(line)
    return b"\n".join(rewritten)


async def stream_response(request, upstream, gateway_session=None, dlp_context=None):
    headers = {k: v for k, v in upstream.headers.items() if k.lower() in ("content-type", "cache-control", "last-event-id")}
    if gateway_session: headers["Mcp-Session-Id"] = gateway_session
    response = web.StreamResponse(status=upstream.status, headers=headers)
    await response.prepare(request)
    async def refresh_session():
        if gateway_session:
            value = await request.app["sessions"].get(gateway_session)
            if value: await request.app["sessions"].set(gateway_session, value)
    if "text/event-stream" in upstream.headers.get("Content-Type", ""):
        pending = b""
        while True:
            try: chunk = await asyncio.wait_for(upstream.content.readany(), timeout=KEEPALIVE)
            except asyncio.TimeoutError:
                await response.write(b": gateway keepalive\n\n")
                await refresh_session()
                continue
            if not chunk:
                if pending:
                    await response.write(await _redact_sse_frame(pending, request, dlp_context) + b"\n\n")
                break
            pending += chunk
            while True:
                boundary = re.search(rb"\r?\n\r?\n", pending)
                if not boundary: break
                frame, separator = pending[:boundary.start()], pending[boundary.start():boundary.end()]
                pending = pending[boundary.end():]
                await response.write(await _redact_sse_frame(frame, request, dlp_context) + separator)
                await refresh_session()
    else:
        raw = await upstream.read()
        if dlp_context:
            counts = {}
            try:
                value = json.loads(raw)
                clean = redact_tree(value, counts)
                raw = json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode()
                audit_value = clean
            except (json.JSONDecodeError, UnicodeDecodeError):
                from security import redact_text
                clean = redact_text(raw.decode("utf-8", errors="replace"), counts)
                raw = clean.encode()
                audit_value = clean
            if counts:
                await audit(request.app, dlp_context["identity"], dlp_context["method"] + ".output", dlp_context["tool"],
                            audit_value, "redacted", "tool output scrubbed", counts)
        await response.write(raw)
    await response.write_eof()
    upstream.release()
    return response


async def mcp_post(request):
    ident = bearer_identity(request)
    try: message = await request.json()
    except Exception: return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return jsonrpc_error(message if isinstance(message, dict) else {}, -32600, "invalid JSON-RPC request", 400)
    method, params = message["method"], message.get("params", {})
    
    # Create request context for middleware pipeline
    gateway_sid = request.headers.get("Mcp-Session-Id")
    request_context = RequestContext(
        request=message,
        identity=ident,
        session_id=gateway_sid,
        start_time=time.perf_counter()
    )
    
    # Process request through middleware pipeline (guardrails, telemetry, etc.)
    pipeline = get_pipeline()
    processed_message, pipeline_error = await pipeline.process_request(message, request_context)
    
    if pipeline_error:
        # Middleware rejected the request (e.g., guardrails blocked a tool call)
        return web.json_response(pipeline_error, status=200)
    
    if method == "tools/list": 
        response = await tools_list(request, processed_message, ident)
        # Process non-streaming JSON responses through pipeline
        if hasattr(response, 'json') and not response.headers.get('Content-Type', '').startswith('text/event-stream'):
            try:
                response_dict = await response.json()
                response_context = ResponseContext(response=response_dict, request_context=request_context)
                processed_response = await pipeline.process_response(response_dict, response_context)
                return web.json_response(processed_response, status=response.status)
            except:
                return response
        return response
    if method == "initialize":
        all_tool_rows = tool_rows()
        rows = [row for row in all_tool_rows if row["transport"] == "streamable-http"]
        endpoints = sorted({row["endpoint"] for row in rows})
        endpoint_auth = {row["endpoint"]: row["auth_env"] for row in rows}
        session_map, first_response = {}, None
        for endpoint in endpoints:
            upstream = await relay(request, endpoint, message, auth_env=endpoint_auth.get(endpoint))
            if upstream.status >= 300:
                upstream.release()
                return jsonrpc_error(message, -32000, "downstream initialization failed", 502)
            backend_sid = upstream.headers.get("Mcp-Session-Id")
            raw = await upstream.read()
            session_map[endpoint] = backend_sid
            if first_response is None:
                first_response = (raw, upstream.headers.get("Content-Type", "application/json"), upstream.status)
        gateway_sid = secrets.token_urlsafe(24)
        tool_rows_for_session = [row for row in all_tool_rows if row["transport"] in ("streamable-http", "rest", "grpc")]
        tool_routes = {row["name"]: row["endpoint"] for row in tool_rows_for_session}
        tool_names = {row["name"]: row["source_tool"] or row["name"] for row in tool_rows_for_session}
        await request.app["sessions"].set(gateway_sid, json.dumps({"user": ident["sub"], "backends": session_map,
            "backend_auth": endpoint_auth, "routes": tool_routes, "upstream_names": tool_names}))
        if first_response is None:
            protocol_version = message.get("params", {}).get("protocolVersion", "2025-03-26")
            return web.json_response({"jsonrpc": "2.0", "id": message.get("id"), "result": {
                "protocolVersion": protocol_version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "secure-mcp-gateway", "version": "1.0"}}}, headers={"Mcp-Session-Id": gateway_sid})
        raw, content_type, status = first_response
        response = web.Response(body=raw, status=status, content_type=content_type.split(";")[0])
        response.headers["Mcp-Session-Id"] = gateway_sid
        return response
    gateway_sid = request.headers.get("Mcp-Session-Id")
    session = await request.app["sessions"].get(gateway_sid) if gateway_sid else None
    if gateway_sid and not session: return jsonrpc_error(message, -32001, "unknown or expired MCP session", 404)
    session = json.loads(session) if session else {"user": ident["sub"], "backends": {}}
    if session["user"] != ident["sub"]: return web.json_response({"error": "session owner mismatch"}, status=403)
    if gateway_sid: await request.app["sessions"].set(gateway_sid, json.dumps(session))
    if method == "notifications/initialized":
        for endpoint, sid in session["backends"].items():
            upstream = await relay(request, endpoint, message, sid, session.get("backend_auth", {}).get(endpoint))
            upstream.release()
        return web.Response(status=202, headers={"Mcp-Session-Id": gateway_sid} if gateway_sid else {})
    if method == "tools/call":
        if not isinstance(params, dict) or not isinstance(params.get("name"), str): return jsonrpc_error(processed_message, -32602, "invalid tool call")
        name = params["name"]
        # Update request context with tool name for telemetry
        request_context.metadata["tool_name"] = name
        row = next((r for r in tool_rows() if r["name"] == name and r["transport"] in ("streamable-http", "rest", "grpc")), None)
        if not row or not (set(ident["roles"]) & set(json.loads(row["write_roles"]))):
            safe_args = redact_tree(params.get("arguments", {}), {})
            await audit(request.app, ident, method, name, safe_args, "deny", "tool unavailable or role denied")
            return jsonrpc_error(processed_message, -32003, "tool unavailable or forbidden")
        args = params.get("arguments", {})
        if not isinstance(args, dict): return jsonrpc_error(processed_message, -32602, "tool arguments must be an object")
        input_redactions = {}
        safe_args = redact_tree(args, input_redactions)
        if INJECTION.search(json.dumps(args, ensure_ascii=False)):
            await audit(request.app, ident, method, name, safe_args, "deny", "payload inspection rule matched", input_redactions)
            return jsonrpc_error(processed_message, -32010, "payload blocked by inspection policy")
        endpoint = session.get("routes", {}).get(name)
        if not endpoint or (row["transport"] == "streamable-http" and endpoint not in session["backends"]):
            return jsonrpc_error(processed_message, -32001, "tool is not part of this MCP session; reinitialize the session", 409)
        if row["transport"] in ("rest", "grpc"):
            try:
                config = json.loads(row["adapter_config"])
                if row["transport"] == "rest":
                    result = await call_rest(request.app, config, safe_args, row["auth_env"])
                    error = result["status"] >= 400
                else:
                    result = await call_grpc(config, safe_args, row["auth_env"])
                    error = False
                output_redactions = {}
                result = redact_tree(result, output_redactions)
                await audit(request.app, ident, method, name, safe_args, "redacted" if input_redactions else "allow",
                            "arguments redacted" if input_redactions else "", input_redactions)
                await audit(request.app, ident, method + ".output", name, result,
                            "redacted" if output_redactions else "allow", "tool output scrubbed" if output_redactions else "",
                            output_redactions)
                content = json.dumps(result, ensure_ascii=False)
                response_result = {"content": [{"type": "text", "text": content}], "structuredContent": result}
                if error: response_result["isError"] = True
                response_dict = {"jsonrpc": "2.0", "id": message.get("id"), "result": response_result}
                
                # Process through middleware pipeline (for truncation, etc.)
                response_context = ResponseContext(response=response_dict, request_context=request_context)
                processed_response = await pipeline.process_response(response_dict, response_context)
                
                response = web.json_response(processed_response)
                if gateway_sid: response.headers["Mcp-Session-Id"] = gateway_sid
                return response
            except Exception as exc:
                await audit(request.app, ident, method, name, safe_args, "error", type(exc).__name__, input_redactions)
                return jsonrpc_error(processed_message, -32000, "API adapter invocation failed", 502)
        try:
            with TRACER.start_as_current_span("mcp.tools.call") as span:
                span.set_attribute("mcp.tool.name", name)
                span.set_attribute("enduser.id", ident["sub"])
                upstream_message = dict(processed_message)
                upstream_message["params"] = dict(params, name=session.get("upstream_names", {}).get(name, name), arguments=safe_args)
                upstream = await relay(request, endpoint, upstream_message, session["backends"].get(endpoint), row["auth_env"])
                span.set_attribute("http.response.status_code", upstream.status)
            verdict = "error" if upstream.status >= 500 else "allow"
            await audit(request.app, ident, method, name, safe_args, "redacted" if input_redactions else verdict,
                        f"input_redactions={input_redactions};downstream_http_status={upstream.status}" if upstream.status >= 400 else "",
                        input_redactions)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            await audit(request.app, ident, method, name, safe_args, "error", type(exc).__name__, input_redactions)
            return jsonrpc_error(processed_message, -32000, "downstream tool unavailable", 502)
        dlp_context = {"identity": ident, "method": method, "tool": name}
        return await stream_response(request, upstream, gateway_sid, dlp_context)
    else:
        endpoint = next(iter(session["backends"]), None) or next((r["endpoint"] for r in tool_rows() if r["transport"] == "streamable-http"), None)
        if not endpoint: return jsonrpc_error(processed_message, -32000, "no downstream MCP server registered", 503)
        upstream = await relay(request, endpoint, processed_message, session["backends"].get(endpoint), session.get("backend_auth", {}).get(endpoint))
    if method == "initialize" and upstream.headers.get("Mcp-Session-Id"):
        session["backends"][endpoint] = upstream.headers["Mcp-Session-Id"]
    # For streaming responses, we skip response processing through pipeline (complex to intercept SSE)
    # Request-level guardrails already applied
    return await stream_response(request, upstream, gateway_sid, {"identity": ident, "method": "mcp.stream", "tool": "session"})


async def mcp_get(request):
    ident = bearer_identity(request)
    gateway_sid = request.headers.get("Mcp-Session-Id")
    raw = await request.app["sessions"].get(gateway_sid) if gateway_sid else None
    if not raw: raise web.HTTPNotFound(text="unknown or expired MCP session")
    session = json.loads(raw)
    if session["user"] != ident["sub"]: raise web.HTTPForbidden(text="session owner mismatch")
    endpoint = next(iter(session["backends"]), None)
    if not endpoint or not session["backends"].get(endpoint): raise web.HTTPNotFound(text="session has no streaming backend session")
    headers = {"Accept": "text/event-stream", "Mcp-Session-Id": session["backends"][endpoint], "MCP-Protocol-Version": request.headers.get("MCP-Protocol-Version", "2025-03-26")}
    auth_env = session.get("backend_auth", {}).get(endpoint)
    if auth_env: headers["Authorization"] = "Bearer " + os.environ[auth_env]
    upstream = await request.app["http"].get(endpoint, headers=headers)
    return await stream_response(request, upstream, gateway_sid, {"identity": ident, "method": "mcp.stream", "tool": "session"})


async def mcp_delete(request):
    ident = bearer_identity(request)
    gateway_sid = request.headers.get("Mcp-Session-Id")
    raw = await request.app["sessions"].get(gateway_sid) if gateway_sid else None
    if not raw: raise web.HTTPNotFound(text="unknown or expired MCP session")
    session = json.loads(raw)
    if session["user"] != ident["sub"]: raise web.HTTPForbidden(text="session owner mismatch")
    for endpoint, sid in session["backends"].items():
        if not sid: continue
        try:
            headers = {"Mcp-Session-Id": sid}
            auth_env = session.get("backend_auth", {}).get(endpoint)
            if auth_env: headers["Authorization"] = "Bearer " + os.environ[auth_env]
            async with request.app["http"].delete(endpoint, headers=headers, allow_redirects=False): pass
        except aiohttp.ClientError: pass
    await request.app["sessions"].delete(gateway_sid)
    return web.Response(status=200)


async def manage_tools(request):
    ident = bearer_identity(request)
    if "mcp.admin" not in ident["roles"]: raise web.HTTPForbidden(text="mcp.admin role required")
    try: data = await request.json()
    except Exception: raise web.HTTPBadRequest(text="invalid JSON body")
    name, endpoint = data.get("name"), data.get("endpoint")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name): raise web.HTTPBadRequest(text="invalid tool name")
    if not isinstance(endpoint, str): raise web.HTTPBadRequest(text="endpoint required")
    transport = data.get("transport", "streamable-http")
    if transport not in ("streamable-http", "sse", "websocket"): raise web.HTTPBadRequest(text="transport must be streamable-http, sse, or websocket; use discovery endpoints for REST/gRPC")
    parsed = urlsplit(endpoint)
    expected_schemes = {"streamable-http": ("http", "https"), "sse": ("http", "https"), "websocket": ("ws", "wss")}[transport]
    if parsed.scheme not in expected_schemes or not parsed.hostname or parsed.username or parsed.password:
        raise web.HTTPBadRequest(text=f"endpoint scheme is invalid for {transport}")
    definition = data.get("tool", {})
    legacy_roles = data.get("roles", ["mcp.engineer"])
    read_roles, write_roles = data.get("read_roles", legacy_roles), data.get("write_roles", legacy_roles)
    if (not isinstance(definition, dict) or not isinstance(read_roles, list) or not isinstance(write_roles, list)
            or not all(isinstance(x, str) for x in read_roles + write_roles)):
        raise web.HTTPBadRequest(text="tool must be an object and read_roles/write_roles string arrays")
    conn = database()
    try:
        conn.execute("INSERT INTO tools(name,endpoint,definition,roles,updated,transport,read_roles,write_roles) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET endpoint=excluded.endpoint, definition=excluded.definition, roles=excluded.roles, updated=excluded.updated, transport=excluded.transport, read_roles=excluded.read_roles, write_roles=excluded.write_roles",
                     (name, endpoint, json.dumps(dict(definition, name=name)), json.dumps(sorted(set(read_roles + write_roles))), int(time.time()), transport,
                      json.dumps(read_roles), json.dumps(write_roles)))
        conn.commit()
    finally: conn.close()
    return web.json_response({"registered": name}, status=201)


def _adapter_roles(data):
    read_roles = data.get("read_roles", ["mcp.engineer"])
    write_roles = data.get("write_roles", ["mcp.engineer"])
    if any(not isinstance(value, list) or not all(isinstance(x, str) for x in value) for value in (read_roles, write_roles)):
        raise web.HTTPBadRequest(text="read_roles and write_roles must be string arrays")
    return read_roles, write_roles


async def create_openapi_adapter(request):
    identity = bearer_identity(request)
    if "mcp.admin" not in identity["roles"]: raise web.HTTPForbidden(text="mcp.admin role required")
    try: data = await request.json()
    except Exception: raise web.HTTPBadRequest(text="invalid JSON body")
    namespace, spec = data.get("name"), data.get("spec")
    if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", namespace): raise web.HTTPBadRequest(text="invalid adapter name")
    if isinstance(spec, str):
        try:
            import yaml
            spec = yaml.safe_load(spec)
        except Exception as exc: raise web.HTTPBadRequest(text=f"invalid OpenAPI document: {type(exc).__name__}") from exc
    if not isinstance(spec, dict): raise web.HTTPBadRequest(text="spec must contain an OpenAPI JSON object")
    try: operations = openapi_operations(spec, namespace, data.get("base_url"))
    except (ValueError, KeyError, TypeError) as exc: raise web.HTTPBadRequest(text=str(exc)) from exc
    if len({item[0] for item in operations}) != len(operations): raise web.HTTPBadRequest(text="OpenAPI operationIds must be unique")
    auth_env = data.get("token_env")
    if auth_env and (not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", auth_env) or not os.getenv(auth_env)):
        raise web.HTTPBadRequest(text="token_env must name a configured environment variable")
    read_roles, write_roles = _adapter_roles(data)
    conn = database()
    try:
        for name, definition, config in operations:
            conn.execute("""INSERT INTO tools(name,endpoint,definition,roles,updated,transport,read_roles,write_roles,auth_env,adapter_config)
              VALUES(?,?,?,?,?,'rest',?,?,?,?) ON CONFLICT(name) DO UPDATE SET endpoint=excluded.endpoint,definition=excluded.definition,
              roles=excluded.roles,updated=excluded.updated,transport=excluded.transport,read_roles=excluded.read_roles,
              write_roles=excluded.write_roles,auth_env=excluded.auth_env,adapter_config=excluded.adapter_config,federation=NULL,source_tool=NULL""",
              (name, config["base_url"], json.dumps(definition), json.dumps(sorted(set(read_roles + write_roles))), int(time.time()),
               json.dumps(read_roles), json.dumps(write_roles), auth_env, json.dumps(config)))
        conn.commit()
    finally: conn.close()
    return web.json_response({"adapter": namespace, "tools": [item[0] for item in operations]}, status=201)


async def create_grpc_adapter(request):
    identity = bearer_identity(request)
    if "mcp.admin" not in identity["roles"]: raise web.HTTPForbidden(text="mcp.admin role required")
    try: data = await request.json()
    except Exception: raise web.HTTPBadRequest(text="invalid JSON body")
    namespace, target = data.get("name"), data.get("target")
    if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", namespace): raise web.HTTPBadRequest(text="invalid adapter name")
    if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9.-]+:\d{1,5}", target): raise web.HTTPBadRequest(text="target must be host:port")
    if "tls" in data and not isinstance(data["tls"], bool): raise web.HTTPBadRequest(text="tls must be a boolean")
    secure, root_cert_env, auth_env = data.get("tls", True), data.get("root_cert_env"), data.get("token_env")
    for env_name in (root_cert_env, auth_env):
        if env_name and (not isinstance(env_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", env_name) or not os.getenv(env_name)):
            raise web.HTTPBadRequest(text="gRPC credential references must name configured environment variables")
    read_roles, write_roles = _adapter_roles(data)
    try: discovered = await asyncio.to_thread(discover_grpc, target, secure, root_cert_env)
    except Exception as exc: raise web.HTTPBadGateway(text=f"gRPC reflection failed: {type(exc).__name__}") from exc
    conn = database()
    names = []
    try:
        for _, definition, config in discovered:
            original_name = definition["name"]
            definition["name"] = f"{namespace}.{original_name}"
            config["source_tool"] = original_name
            config["namespace"] = namespace
            config["auth_env"] = auth_env
            name = definition["name"]
            conn.execute("""INSERT INTO tools(name,endpoint,definition,roles,updated,transport,read_roles,write_roles,auth_env,adapter_config)
              VALUES(?,?,?,?,?,'grpc',?,?,?,?) ON CONFLICT(name) DO UPDATE SET endpoint=excluded.endpoint,definition=excluded.definition,
              roles=excluded.roles,updated=excluded.updated,transport=excluded.transport,read_roles=excluded.read_roles,
              write_roles=excluded.write_roles,auth_env=excluded.auth_env,adapter_config=excluded.adapter_config,federation=NULL,source_tool=NULL""",
              (name, target, json.dumps(definition), json.dumps(sorted(set(read_roles + write_roles))), int(time.time()),
               json.dumps(read_roles), json.dumps(write_roles), auth_env, json.dumps(config)))
            names.append(name)
        conn.commit()
    finally: conn.close()
    return web.json_response({"adapter": namespace, "tools": names}, status=201)


async def list_tools(request):
    ident = bearer_identity(request)
    return web.json_response({"tools": [json.loads(r["definition"]) for r in tool_rows() if r["transport"] in ("streamable-http", "rest", "grpc") and set(ident["roles"]) & set(json.loads(r["read_roles"]))]})


async def delete_tool(request):
    ident = bearer_identity(request)
    if "mcp.admin" not in ident["roles"]: raise web.HTTPForbidden(text="mcp.admin role required")
    conn = database()
    try:
        cursor = conn.execute("DELETE FROM tools WHERE name=?", (request.match_info["name"],))
        conn.commit()
    finally: conn.close()
    if cursor.rowcount == 0: raise web.HTTPNotFound(text="tool not found")
    return web.Response(status=204)


def federation_record(name):
    conn = database()
    try: return conn.execute("SELECT * FROM federations WHERE name=?", (name,)).fetchone()
    finally: conn.close()


async def sync_federation(app, name):
    source = federation_record(name)
    if not source: raise web.HTTPNotFound(text="federation not found")
    token = os.getenv(source["token_env"])
    if not token: raise web.HTTPServiceUnavailable(text="federation token environment variable is not configured")
    async with app["http"].get(source["tools_url"], headers={"Authorization": "Bearer " + token, "Accept": "application/json"}) as response:
        if response.status != 200: raise web.HTTPBadGateway(text=f"upstream registry returned HTTP {response.status}")
        registry = await response.json()
    remote_tools = registry.get("tools") if isinstance(registry, dict) else None
    if not isinstance(remote_tools, list): raise web.HTTPBadGateway(text="upstream registry response must contain a tools array")
    read_roles, write_roles = json.loads(source["read_roles"]), json.loads(source["write_roles"])
    normalized = []
    for definition in remote_tools:
        if not isinstance(definition, dict) or not isinstance(definition.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", definition["name"]):
            raise web.HTTPBadGateway(text="upstream registry returned an invalid tool")
        normalized.append((definition["name"], dict(definition, name=f"{name}.{definition['name']}")))
    conn = database()
    try:
        old = conn.execute("SELECT name,source_tool FROM tools WHERE federation=?", (name,)).fetchall()
        current_names = {source_tool for source_tool, _ in normalized}
        for row in old:
            if row["source_tool"] not in current_names: conn.execute("DELETE FROM tools WHERE name=?", (row["name"],))
        for source_tool, definition in normalized:
            collision = conn.execute("SELECT federation FROM tools WHERE name=?", (definition["name"],)).fetchone()
            if collision and collision["federation"] != name:
                raise web.HTTPConflict(text=f"federated tool name collision: {definition['name']}")
            roles = json.dumps(sorted(set(read_roles + write_roles)))
            conn.execute("""INSERT INTO tools(name,endpoint,definition,roles,updated,transport,read_roles,write_roles,auth_env,federation,source_tool)
              VALUES(?,?,?,?,?,'streamable-http',?,?,?,?,?)
              ON CONFLICT(name) DO UPDATE SET endpoint=excluded.endpoint,definition=excluded.definition,roles=excluded.roles,
              updated=excluded.updated,transport=excluded.transport,read_roles=excluded.read_roles,write_roles=excluded.write_roles,
              auth_env=excluded.auth_env,federation=excluded.federation,source_tool=excluded.source_tool""",
              (definition["name"], source["mcp_url"], json.dumps(definition), roles, int(time.time()),
               json.dumps(read_roles), json.dumps(write_roles), source["token_env"], name, source_tool))
        conn.execute("UPDATE federations SET updated=? WHERE name=?", (int(time.time()), name))
        conn.commit()
    finally: conn.close()
    return len(normalized)


async def register_federation(request):
    identity = bearer_identity(request)
    if "mcp.admin" not in identity["roles"]: raise web.HTTPForbidden(text="mcp.admin role required")
    try: data = await request.json()
    except Exception: raise web.HTTPBadRequest(text="invalid JSON body")
    return await save_federation(request, data)


async def save_federation(request, data):
    name, tools_url, mcp_url, token_env = (data.get(k) for k in ("name", "tools_url", "mcp_url", "token_env"))
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name): raise web.HTTPBadRequest(text="invalid federation name")
    if not isinstance(token_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", token_env) or not os.getenv(token_env):
        raise web.HTTPBadRequest(text="token_env must name a configured environment variable")
    for value in (tools_url, mcp_url):
        parsed = urlsplit(value or "")
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise web.HTTPBadRequest(text="federation URLs must be HTTP(S) URLs without embedded credentials")
    read_roles, write_roles = data.get("read_roles", ["mcp.engineer"]), data.get("write_roles", ["mcp.engineer"])
    if any(not isinstance(r, list) or not all(isinstance(x, str) for x in r) for r in (read_roles, write_roles)):
        raise web.HTTPBadRequest(text="read_roles and write_roles must be string arrays")
    conn = database()
    try:
        existing_auth = conn.execute("SELECT DISTINCT auth_env FROM tools WHERE endpoint=?", (mcp_url,)).fetchall()
        if any(row["auth_env"] != token_env for row in existing_auth):
            raise web.HTTPConflict(text="all federated tools sharing an MCP endpoint must use the same token_env")
        conn.execute("INSERT INTO federations VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET tools_url=excluded.tools_url,mcp_url=excluded.mcp_url,token_env=excluded.token_env,read_roles=excluded.read_roles,write_roles=excluded.write_roles,updated=excluded.updated",
                     (name, tools_url, mcp_url, token_env, json.dumps(read_roles), json.dumps(write_roles), int(time.time())))
        conn.commit()
    finally: conn.close()
    count = await sync_federation(request.app, name)
    return web.json_response({"federation": name, "synced_tools": count}, status=201)


async def refresh_federation(request):
    identity = bearer_identity(request)
    if "mcp.admin" not in identity["roles"]: raise web.HTTPForbidden(text="mcp.admin role required")
    count = await sync_federation(request.app, request.match_info["name"])
    return web.json_response({"federation": request.match_info["name"], "synced_tools": count})


async def delete_federation(request):
    identity = bearer_identity(request)
    if "mcp.admin" not in identity["roles"]: raise web.HTTPForbidden(text="mcp.admin role required")
    name = request.match_info["name"]
    conn = database()
    try:
        conn.execute("DELETE FROM tools WHERE federation=?", (name,))
        result = conn.execute("DELETE FROM federations WHERE name=?", (name,))
        conn.commit()
    finally: conn.close()
    if not result.rowcount: raise web.HTTPNotFound(text="federation not found")
    return web.Response(status=204)


async def list_federations(request):
    identity = bearer_identity(request)
    if "mcp.admin" not in identity["roles"]: raise web.HTTPForbidden(text="mcp.admin role required")
    conn = database()
    try:
        rows = conn.execute("SELECT name,tools_url,mcp_url,token_env,read_roles,write_roles,updated FROM federations ORDER BY name").fetchall()
        result = [{**dict(row), "read_roles": json.loads(row["read_roles"]), "write_roles": json.loads(row["write_roles"])} for row in rows]
    finally: conn.close()
    return web.json_response({"federations": result})


# A peer is an independently administered gateway. Peers use the same
# authenticated registry and MCP transport as upstream federations, with a
# locally configured OAuth bearer credential and explicit local role mapping.
async def list_peers(request):
    response = await list_federations(request)
    payload = json.loads(response.text)
    payload["peers"] = payload.pop("federations")
    return web.json_response(payload)


async def register_peer(request):
    identity = bearer_identity(request)
    if "mcp.admin" not in identity["roles"]: raise web.HTTPForbidden(text="mcp.admin role required")
    try: data = await request.json()
    except Exception: raise web.HTTPBadRequest(text="invalid JSON body")
    for key in ("registry_url", "mcp_url"):
        if key not in data: raise web.HTTPBadRequest(text=f"{key} is required")
    normalized = {"name": data.get("name"), "tools_url": data["registry_url"],
                  "mcp_url": data["mcp_url"], "token_env": data.get("credential_env"),
                  "read_roles": data.get("read_roles", ["mcp.engineer"]),
                  "write_roles": data.get("write_roles", ["mcp.engineer"])}
    result = await save_federation(request, normalized)
    payload = json.loads(result.text)
    return web.json_response({"peer": payload["federation"], "synced_tools": payload["synced_tools"]}, status=201)


async def sync_peer(request):
    identity = bearer_identity(request)
    if "mcp.admin" not in identity["roles"]: raise web.HTTPForbidden(text="mcp.admin role required")
    name = request.match_info["name"]
    count = await sync_federation(request.app, name)
    return web.json_response({"peer": name, "synced_tools": count})


async def delete_peer(request):
    return await delete_federation(request)


async def sse_adapter(request):
    ident = bearer_identity(request)
    name = request.match_info["name"]
    row = next((r for r in tool_rows() if r["name"] == name), None)
    if not row or row["transport"] != "sse": raise web.HTTPNotFound()
    if not (set(ident["roles"]) & set(json.loads(row["read_roles"]))): raise web.HTTPForbidden()
    upstream = await request.app["http"].get(row["endpoint"], headers={"Accept": "text/event-stream"})
    return await stream_response(request, upstream, dlp_context={"identity": ident, "method": "sse.proxy", "tool": name})


async def websocket_adapter(request):
    ident = bearer_identity(request)
    name = request.match_info["name"]
    row = next((r for r in tool_rows() if r["name"] == name), None)
    if not row or row["transport"] != "websocket" or not (set(ident["roles"]) & set(json.loads(row["write_roles"]))): raise web.HTTPForbidden()
    parsed = urlsplit(row["endpoint"])
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    target = f"{scheme}://{parsed.netloc}{parsed.path}"
    client = web.WebSocketResponse(heartbeat=KEEPALIVE)
    await client.prepare(request)
    async with request.app["http"].ws_connect(target, headers={"X-MCP-User": ident["sub"]}) as upstream:
        async def pump(source, dest):
            async for msg in source:
                if msg.type == WSMsgType.TEXT: await dest.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY: await dest.send_bytes(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR): break
        await asyncio.gather(pump(client, upstream), pump(upstream, client))
    return client


async def health(_): return web.json_response({"status": "ok"})


@web.middleware
async def telemetry_middleware(request, handler):
    started = time.perf_counter()
    status = 500
    with TRACER.start_as_current_span("http.server.request") as span:
        span.set_attribute("http.request.method", request.method)
        try:
            response = await handler(request)
            status = response.status
            return response
        except web.HTTPException as exc:
            status = exc.status
            raise
        finally:
            attributes = {"http.request.method": request.method, "http.response.status_code": status}
            REQUESTS.add(1, attributes)
            LATENCY.record((time.perf_counter() - started) * 1000, attributes)
            span.set_attribute("http.response.status_code", status)


async def startup(app):
    if bool(os.getenv("MCP_AUDIT_WEBHOOK_URL")) != bool(os.getenv("MCP_SPLUNK_HEC_TOKEN")):
        raise RuntimeError("Configure both MCP_AUDIT_WEBHOOK_URL and MCP_SPLUNK_HEC_TOKEN for HEC delivery")
    app["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=10), auto_decompress=False)
    app["sessions"] = SessionStore()
    if app["sessions"].redis:
        await app["sessions"].redis.ping()
    app["logger"] = __import__("logging").getLogger("gateway")
    database().close()
    app["audit_worker"] = asyncio.create_task(audit_delivery_worker(app)) if os.getenv("MCP_AUDIT_WEBHOOK_URL") else None
    
    # Initialize middleware pipeline
    pipeline = get_pipeline()
    pipeline.use(TelemetryMiddleware())
    pipeline.use(GuardrailsMiddleware())
    app["logger"].info("Middleware pipeline initialized with Telemetry and Guardrails")


async def cleanup(app):
    if app.get("audit_worker"):
        app["audit_worker"].cancel()
        try: await app["audit_worker"]
        except asyncio.CancelledError: pass
    await app["http"].close()
    if app["sessions"].redis: await app["sessions"].redis.aclose()


def create_app():
    app = web.Application(client_max_size=MAX_BODY, middlewares=[telemetry_middleware])
    app.router.add_get("/health", health)
    app.router.add_get("/tools", list_tools)
    app.router.add_post("/tools", manage_tools)
    app.router.add_delete("/tools/{name}", delete_tool)
    app.router.add_post("/adapters/openapi", create_openapi_adapter)
    app.router.add_post("/adapters/grpc", create_grpc_adapter)
    app.router.add_get("/federations", list_federations)
    app.router.add_post("/federations", register_federation)
    app.router.add_post("/federations/{name}/sync", refresh_federation)
    app.router.add_delete("/federations/{name}", delete_federation)
    app.router.add_get("/peers", list_peers)
    app.router.add_post("/peers", register_peer)
    app.router.add_post("/peers/{name}/sync", sync_peer)
    app.router.add_delete("/peers/{name}", delete_peer)
    app.router.add_post("/mcp", mcp_post)
    app.router.add_get("/mcp", mcp_get)
    app.router.add_delete("/mcp", mcp_delete)
    app.router.add_get("/sse/{name}", sse_adapter)
    app.router.add_get("/ws/{name}", websocket_adapter)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host=os.getenv("MCP_GATEWAY_HOST", "127.0.0.1"), port=int(os.getenv("MCP_GATEWAY_PORT", "8080")), shutdown_timeout=5)
