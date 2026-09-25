import asyncio
import threading
from concurrent import futures
from aiohttp import web, WSMsgType
import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from grpc_reflection.v1alpha import reflection

hec_events = []
otlp_paths = []

async def events(_request):
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await response.prepare(_request)
    await asyncio.sleep(3600)
    return response

async def socket(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for message in ws:
        if message.type == WSMsgType.TEXT:
            await ws.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            await ws.send_bytes(message.data)
    return ws

async def remote_tools(request):
    if request.headers.get("Authorization") != "Bearer upstream-test-token":
        raise web.HTTPUnauthorized()
    return web.json_response({"tools": [{"name": "multiply", "description": "Multiply two values",
                                         "inputSchema": {"type": "object"}}]})

async def remote_mcp(request):
    if request.headers.get("Authorization") != "Bearer upstream-test-token":
        raise web.HTTPUnauthorized()
    message = await request.json()
    method = message.get("method")
    if method == "initialize":
        response = web.json_response({"jsonrpc": "2.0", "id": message.get("id"), "result": {
            "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
            "serverInfo": {"name": "remote-registry", "version": "1"}}})
        response.headers["Mcp-Session-Id"] = "remote-session"
        return response
    if method == "notifications/initialized": return web.Response(status=202)
    if method == "tools/call":
        args = message["params"]["arguments"]
        value = args["a"] * args["b"]
        return web.json_response({"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": str(value)}], "structuredContent": {"result": value}}})
    return web.json_response({"jsonrpc": "2.0", "id": message.get("id"), "result": {}})

async def hec(request):
    if request.headers.get("Authorization") != "Splunk test-hec-token":
        raise web.HTTPUnauthorized()
    hec_events.append(await request.json())
    return web.json_response({"text": "Success", "code": 0})

async def otlp(request):
    otlp_paths.append(request.path)
    await request.read()
    return web.Response(status=200)

async def observability(_request):
    return web.json_response({"hec": hec_events, "otlp": otlp_paths})

async def legacy_user(request):
    return web.json_response({"userId": request.match_info["user_id"], "email": "sensitive@example.org"})

def start_grpc():
    file = descriptor_pb2.FileDescriptorProto(name="legacy.proto", package="legacy", syntax="proto3")
    request = file.message_type.add(name="MultiplyRequest")
    request.field.add(name="a", number=1, label=1, type=5)
    request.field.add(name="b", number=2, label=1, type=5)
    response = file.message_type.add(name="MultiplyResponse")
    response.field.add(name="value", number=1, label=1, type=5)
    service = file.service.add(name="Calculator")
    service.method.add(name="Multiply", input_type=".legacy.MultiplyRequest", output_type=".legacy.MultiplyResponse")
    pool = descriptor_pool.Default()
    try: pool.Add(file)
    except Exception: pass
    request_cls = message_factory.GetMessageClass(pool.FindMessageTypeByName("legacy.MultiplyRequest"))
    response_cls = message_factory.GetMessageClass(pool.FindMessageTypeByName("legacy.MultiplyResponse"))
    def multiply(req, context):
        return response_cls(value=req.a * req.b)
    handler = grpc.method_handlers_generic_handler("legacy.Calculator", {
        "Multiply": grpc.unary_unary_rpc_method_handler(multiply, request_deserializer=request_cls.FromString,
                                                          response_serializer=lambda value: value.SerializeToString())})
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((handler,))
    reflection.enable_server_reflection(("legacy.Calculator", reflection.SERVICE_NAME), server)
    server.add_insecure_port("127.0.0.1:8003")
    server.start()
    return server

app = web.Application()
app.router.add_get("/events", events)
app.router.add_get("/socket", socket)
app.router.add_get("/registry/tools", remote_tools)
app.router.add_post("/registry/mcp", remote_mcp)
app.router.add_post("/services/collector/event", hec)
app.router.add_post("/v1/{tail:.*}", otlp)
app.router.add_get("/observability", observability)
app.router.add_get("/legacy/users/{user_id}", legacy_user)
grpc_server = start_grpc()
web.run_app(app, host="127.0.0.1", port=8001)
