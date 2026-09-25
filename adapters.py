"""OpenAPI-to-MCP and gRPC-reflection adapter discovery and invocation."""
from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.parse import quote, urljoin, urlsplit

import aiohttp

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head"}


def _resolve_refs(value, components, depth=0):
    if depth > 24: raise ValueError("OpenAPI schema reference depth exceeded")
    if isinstance(value, list): return [_resolve_refs(x, components, depth + 1) for x in value]
    if not isinstance(value, dict): return value
    ref = value.get("$ref")
    if ref:
        if not ref.startswith("#/components/"): raise ValueError("only local OpenAPI component references are supported")
        target = components
        for part in ref.removeprefix("#/components/").split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target[part]
        merged = dict(target)
        merged.update({k: v for k, v in value.items() if k != "$ref"})
        return _resolve_refs(merged, components, depth + 1)
    return {key: _resolve_refs(item, components, depth + 1) for key, item in value.items()}


def _safe_base_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("OpenAPI base URL must be HTTP(S) without embedded credentials")
    if "{" in url or "}" in url: raise ValueError("OpenAPI server URL variables must be resolved with base_url")
    return url.rstrip("/") + "/"


def openapi_operations(spec, namespace, base_url=None):
    if not isinstance(spec, dict) or not str(spec.get("openapi", "")).startswith("3."):
        raise ValueError("an OpenAPI 3.x specification is required")
    servers = spec.get("servers") or []
    server_url = base_url or (servers[0].get("url") if servers else None)
    if not server_url: raise ValueError("base_url or OpenAPI servers[0].url is required")
    base_url = _safe_base_url(server_url)
    components = spec.get("components", {})
    result = []
    for path, path_item in spec.get("paths", {}).items():
        inherited = _resolve_refs(path_item.get("parameters", []), components)
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict): continue
            operation = _resolve_refs(operation, components)
            operation_id = operation.get("operationId") or f"{method.lower()}_{path.strip('/').replace('/', '_').replace('{','').replace('}','')}"
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", operation_id): raise ValueError(f"invalid OpenAPI operationId: {operation_id}")
            parameters = inherited + operation.get("parameters", [])
            groups = {"path": {}, "query": {}, "header": {}}
            required = {"path": [], "query": [], "header": []}
            for parameter in parameters:
                location, name = parameter.get("in"), parameter.get("name")
                if location not in groups or not name: continue
                schema = parameter.get("schema", {"type": "string"})
                groups[location][name] = schema
                if parameter.get("required") or location == "path": required[location].append(name)
            props, required_top = {}, []
            for location, field in (("path", "path_params"), ("query", "query"), ("header", "headers")):
                if groups[location]:
                    props[field] = {"type": "object", "properties": groups[location], "additionalProperties": False}
                    if required[location]: props[field]["required"] = required[location]
                    if required[location]: required_top.append(field)
            body_schema = None
            request_body = operation.get("requestBody", {})
            content = request_body.get("content", {}) if isinstance(request_body, dict) else {}
            if "application/json" in content:
                body_schema = content["application/json"].get("schema", {"type": "object"})
                props["body"] = body_schema
                if request_body.get("required"): required_top.append("body")
            input_schema = {"type": "object", "properties": props, "additionalProperties": False}
            if required_top: input_schema["required"] = required_top
            safe_operation = {"method": method.upper(), "path": path, "base_url": base_url,
                              "path_params": list(groups["path"]), "query_params": list(groups["query"]),
                              "header_params": list(groups["header"]), "has_body": body_schema is not None,
                              "input_schema": input_schema}
            tool_name = f"{namespace}.{operation_id}"
            definition = {"name": tool_name, "description": operation.get("summary") or operation.get("description") or f"{method.upper()} {path}",
                          "inputSchema": input_schema}
            result.append((tool_name, definition, safe_operation))
    if not result: raise ValueError("OpenAPI specification has no supported operations")
    return result


async def call_rest(app, operation, args, auth_env=None):
    import jsonschema
    jsonschema.validate(args, operation["input_schema"])
    path = operation["path"]
    path_params = args.get("path_params", {})
    for key in re.findall(r"\{([^{}]+)\}", path):
        if key not in path_params: raise ValueError(f"missing path parameter: {key}")
        path = path.replace("{" + key + "}", quote(str(path_params[key]), safe=""))
    url = urljoin(operation["base_url"], path.lstrip("/"))
    query = {key: value for key, value in args.get("query", {}).items() if key in operation["query_params"]}
    forbidden_headers = {"authorization", "host", "cookie", "content-length", "connection", "transfer-encoding"}
    headers = {key: str(value) for key, value in args.get("headers", {}).items()
               if key.lower() in {x.lower() for x in operation["header_params"]} and key.lower() not in forbidden_headers
               and "\r" not in str(value) and "\n" not in str(value)}
    if auth_env:
        token = os.getenv(auth_env)
        if not token: raise RuntimeError("REST adapter token environment variable is missing")
        headers["Authorization"] = "Bearer " + token
    body = args.get("body") if operation["has_body"] else None
    async with app["http"].request(operation["method"], url, params=query, headers=headers, json=body, allow_redirects=False) as response:
        raw = await response.read()
        if len(raw) > int(os.getenv("MCP_ADAPTER_MAX_RESPONSE", "2097152")): raise ValueError("REST adapter response too large")
        try: output = json.loads(raw) if raw else None
        except json.JSONDecodeError: output = raw.decode(response.charset or "utf-8", errors="replace")
        return {"status": response.status, "body": output}


def discover_grpc(target, secure=False, root_cert_env=None):
    import grpc
    from google.protobuf import descriptor_pool, message_factory
    from grpc_reflection.v1alpha.proto_reflection_descriptor_database import ProtoReflectionDescriptorDatabase

    credentials = None
    if secure:
        pem = os.getenv(root_cert_env) if root_cert_env else None
        credentials = grpc.ssl_channel_credentials(root_certificates=pem.encode() if pem else None)
        channel = grpc.secure_channel(target, credentials)
    else:
        channel = grpc.insecure_channel(target)
    try:
        reflection_db = ProtoReflectionDescriptorDatabase(channel)
        pool = descriptor_pool.DescriptorPool(reflection_db)
        tools = []
        for service_name in reflection_db.get_services():
            if service_name.startswith("grpc.reflection."): continue
            descriptor = pool.FindServiceByName(service_name)
            for method in descriptor.methods:
                if method.client_streaming or method.server_streaming: continue
                schema = _message_schema(method.input_type)
                short = f"{service_name}.{method.name}"
                name = re.sub(r"[^A-Za-z0-9_.-]", "_", short)
                definition = {"name": name, "description": f"Invoke {short}", "inputSchema": schema}
                config = {"target": target, "service": service_name, "method": method.name,
                          "input_type": method.input_type.full_name, "output_type": method.output_type.full_name,
                          "secure": secure, "root_cert_env": root_cert_env}
                tools.append((name, definition, config))
        return tools
    finally: channel.close()


def _message_schema(descriptor, depth=0):
    from google.protobuf.descriptor import FieldDescriptor
    if depth > 16: return {"type": "object"}
    scalar = {FieldDescriptor.TYPE_STRING: "string", FieldDescriptor.TYPE_BYTES: "string",
              FieldDescriptor.TYPE_BOOL: "boolean", FieldDescriptor.TYPE_INT32: "integer",
              FieldDescriptor.TYPE_INT64: "integer", FieldDescriptor.TYPE_UINT32: "integer",
              FieldDescriptor.TYPE_UINT64: "integer", FieldDescriptor.TYPE_SINT32: "integer",
              FieldDescriptor.TYPE_SINT64: "integer", FieldDescriptor.TYPE_FIXED32: "integer",
              FieldDescriptor.TYPE_FIXED64: "integer", FieldDescriptor.TYPE_SFIXED32: "integer",
              FieldDescriptor.TYPE_SFIXED64: "integer", FieldDescriptor.TYPE_FLOAT: "number",
              FieldDescriptor.TYPE_DOUBLE: "number"}
    props, required = {}, []
    for field in descriptor.fields:
        if field.message_type and field.message_type.GetOptions().map_entry:
            key, value = field.message_type.fields
            value_schema = _message_schema(value.message_type, depth + 1) if value.message_type else {"type": scalar.get(value.type, "string")}
            schema = {"type": "object", "additionalProperties": value_schema}
        elif field.type == FieldDescriptor.TYPE_ENUM:
            schema = {"type": "string", "enum": [item.name for item in field.enum_type.values]}
        elif field.type == FieldDescriptor.TYPE_MESSAGE:
            schema = _message_schema(field.message_type, depth + 1)
        else:
            schema = {"type": scalar.get(field.type, "string")}
        is_repeated = getattr(field, "is_repeated", None)
        if (is_repeated if is_repeated is not None else field.label == FieldDescriptor.LABEL_REPEATED) and not (field.message_type and field.message_type.GetOptions().map_entry):
            schema = {"type": "array", "items": schema}
        props[field.json_name] = schema
        is_required = getattr(field, "is_required", None)
        if (is_required if is_required is not None else field.label == FieldDescriptor.LABEL_REQUIRED): required.append(field.json_name)
    result = {"type": "object", "properties": props, "additionalProperties": False}
    if required: result["required"] = required
    return result


def invoke_grpc(config, args, auth_env=None):
    import grpc
    from google.protobuf import descriptor_pool, json_format, message_factory
    from grpc_reflection.v1alpha.proto_reflection_descriptor_database import ProtoReflectionDescriptorDatabase
    target, secure = config["target"], config["secure"]
    if secure:
        pem = os.getenv(config.get("root_cert_env")) if config.get("root_cert_env") else None
        channel = grpc.secure_channel(target, grpc.ssl_channel_credentials(root_certificates=pem.encode() if pem else None))
    else:
        channel = grpc.insecure_channel(target)
    try:
        pool = descriptor_pool.DescriptorPool(ProtoReflectionDescriptorDatabase(channel))
        request_cls = message_factory.GetMessageClass(pool.FindMessageTypeByName(config["input_type"]))
        response_cls = message_factory.GetMessageClass(pool.FindMessageTypeByName(config["output_type"]))
        message = json_format.ParseDict(args, request_cls())
        method = channel.unary_unary(f"/{config['service']}/{config['method']}",
                                    request_serializer=lambda value: value.SerializeToString(),
                                    response_deserializer=response_cls.FromString)
        metadata = [("authorization", "Bearer " + os.environ[auth_env])] if auth_env else None
        result = method(message, timeout=float(os.getenv("MCP_GRPC_TIMEOUT_SECONDS", "30")), metadata=metadata)
        return json_format.MessageToDict(result, preserving_proto_field_name=True)
    finally: channel.close()


async def call_grpc(config, args, auth_env=None):
    return await asyncio.to_thread(invoke_grpc, config, args, auth_env)
