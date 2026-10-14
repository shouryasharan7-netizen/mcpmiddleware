# Building Secure MCP Gateways with Guardrails and Middleware

The Model Context Protocol (MCP) is revolutionizing how AI assistants interact with external tools and APIs. But as organizations adopt MCP at scale, security concerns emerge: How do you prevent AI from executing dangerous commands? How do you control which tools can be accessed? How do you protect sensitive systems?

Today, I'm excited to share the **MCP Middleware Gateway** — a production-grade proxy that adds pluggable guardrails, telemetry, and security policies to any MCP deployment.

## The Problem: MCP Needs Guardrails

MCP is designed for flexibility — but flexibility without control is dangerous. Consider these scenarios:

- An AI agent tries to execute `rm -rf /etc` through a bash tool
- A user requests deletion of production databases
- Large responses overflow context windows and break conversations
- No visibility into which tools are being called and by whom

We need a way to intercept, inspect, and control MCP traffic without breaking the protocol.

## The Solution: Pluggable Middleware Pipeline

The MCP Middleware Gateway introduces a clean, extensible middleware pipeline that sits between MCP clients (like Claude Desktop) and MCP servers. Every request and response passes through registered middleware hooks:

```python
class Middleware:
    async def before_request(self, request, context):
        # Intercept and modify/reject requests
        pass
    
    async def after_response(self, response, context):
        # Intercept and modify responses
        pass
    
    async def on_error(self, error, context):
        # Handle errors
        pass
```

This pattern enables endless customization while maintaining MCP JSON-RPC compliance.

## Built-in Guardrails

The gateway ships with production-ready guardrails out of the box:

### 1. Tool Name Blocking

```bash
export MCP_BLOCKED_TOOLS='["dangerous_tool", "delete_database"]'
```

Prevent access to specific tools entirely.

### 2. Pattern Matching

```bash
export MCP_BLOCKED_PATTERNS='["rm -rf", "sudo ", "docker rm", "kubectl delete"]'
```

Block dangerous commands in tool arguments using regex patterns.

### 3. Protected Path Detection

```bash
export MCP_PROTECTED_PATHS='["/etc", "/usr", "/var", "/home", "/root"]'
```

Prevent tool access to sensitive filesystem paths.

### 4. Output Truncation

```bash
export MCP_MAX_RESPONSE_SIZE="2097152"  # 2MB
```

Automatically truncate oversized responses to prevent context overflow.

## Real-World Example

Here's what happens when an AI tries to execute a dangerous command:

```
Client: {"jsonrpc":"2.0","method":"tools/call","params":{"name":"bash","arguments":{"command":"rm -rf /etc"}}}
Gateway: Guardrails detects "rm -rf" pattern
Gateway: Returns JSON-RPC error -32098
Client: ✅ Request blocked, system protected
```

The gateway returns a standard JSON-RPC error response:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32098,
    "message": "Tool call blocked by guardrails policy",
    "data": {
      "reason": "Argument matches blocked pattern: rm -rf"
    }
  }
}
```

## Lightweight Telemetry

Understanding what's happening in your MCP deployment is critical. The gateway logs:

- Tool execution latency
- Method names
- Packet byte sizes
- Success/failure status
- User identity

All logged to stderr by default (configurable to any logger):

```
2025-01-13 10:23:45 - gateway.telemetry - INFO - REQUEST method=tools/call tool=bash user=test@example.com size=245 bytes
2025-01-13 10:23:46 - gateway.telemetry - INFO - RESPONSE method=tools/call tool=bash user=test@example.com latency=1234.56ms size=1892 bytes status=success
```

## Custom Middleware Examples

The pipeline is designed for extensibility. Here are some examples you can build:

### Rate Limiting

```python
class RateLimitMiddleware(Middleware):
    async def before_request(self, request, context):
        user = context.identity.get("sub")
        if self.user_requests[user] > self.limit:
            return None  # Reject
        self.user_requests[user] += 1
        return request
```

### Caching

```python
class CachingMiddleware(Middleware):
    async def before_request(self, request, context):
        cache_key = self.hash(request)
        if cache_key in self.cache:
            return self.cache[cache_key]
        return request
```

### Request Transformation

```python
class TransformationMiddleware(Middleware):
    async def before_request(self, request, context):
        # Add authentication headers
        request["params"]["auth_token"] = self.get_token()
        return request
```

## Architecture

```
Client → Gateway → Middleware Pipeline → Auth → MCP Server
         ↓              ↓                    ↓
      Guardrails    Telemetry          Registry
         ↓              ↓                    ↓
    Pattern      Latency Logs        Role Checks
    Matching
```

The pipeline processes requests in registration order (Telemetry → Guardrails → Custom) and responses in reverse order.

## Quick Start

```bash
# Clone and run
git clone https://github.com/shouryasharan7-netizen/mcpmiddleware
cd mcpmiddleware
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# Configure guardrails
export MCP_BLOCKED_PATTERNS='["rm -rf", "sudo "]'
export MCP_MAX_RESPONSE_SIZE="2097152"

# Run
python main.py
```

## Claude Desktop Integration

Configure Claude Desktop to use the gateway:

```json
{
  "mcpServers": {
    "secure-gateway": {
      "command": "python",
      "args": ["/path/to/mcpmiddleware/main.py"],
      "env": {
        "MCP_BLOCKED_PATTERNS": "[\"rm -rf\", \"sudo \"]"
      }
    }
  }
}
```

## What's Next?

This is just the beginning. The roadmap includes:

- Rate limiting middleware
- Prometheus metrics export
- Multi-tenant support
- Advanced anomaly detection
- Plugin marketplace

## Get Involved

The project is open source and welcomes contributions:

- GitHub: https://github.com/shouryasharan7-netizen/mcpmiddleware
- Issues: https://github.com/shouryasharan7-netizen/mcpmiddleware/issues
- Documentation: https://github.com/shouryasharan7-netizen/mcpmiddleware/blob/main/README.md

## Conclusion

Security shouldn't be an afterthought in AI systems. The MCP Middleware Gateway provides a production-grade solution for adding guardrails, telemetry, and control to any MCP deployment — without breaking the protocol or adding complexity.

Try it out, and let me know what middleware you build!

---

*Built with love for the MCP community. 🚀*
