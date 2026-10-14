# Quick Start Guide

Get the MCP Middleware Gateway running in **5 minutes** with guardrails enabled.

## Option 1: Docker Compose (Recommended - Easiest)

### Prerequisites
- Docker and Docker Compose installed

### Steps

1. **Clone the repository**
   ```bash
   git clone https://github.com/shouryasharan7-netizen/mcpmiddleware.git
   cd mcpmiddleware
   ```

2. **Start with Docker Compose**
   ```bash
   docker-compose up -d
   ```

3. **Verify it's running**
   ```bash
   curl http://localhost:8080/health
   # Should return: {"status": "ok"}
   ```

4. **Test guardrails**
   ```bash
   # Try a blocked command (will be rejected)
   curl -X POST http://localhost:8080/mcp \
     -H "Content-Type: application/json" \
     -d '{
       "jsonrpc": "2.0",
       "id": 1,
       "method": "tools/call",
       "params": {
         "name": "bash",
         "arguments": {"command": "rm -rf /etc"}
       }
     }'
   ```

5. **View logs**
   ```bash
   docker-compose logs -f gateway
   ```

6. **Stop when done**
   ```bash
   docker-compose down
   ```

**That's it!** The gateway is now running with:
- ✅ Guardrails blocking dangerous commands
- ✅ Redis for session storage
- ✅ Telemetry logging
- ✅ Output truncation

---

## Option 2: Python (No Docker)

### Prerequisites
- Python 3.10 or newer
- pip

### Steps

1. **Clone the repository**
   ```bash
   git clone https://github.com/shouryasharan7-netizen/mcpmiddleware.git
   cd mcpmiddleware
   ```

2. **Install dependencies**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Run with minimal config**
   ```bash
   export MCP_ALLOW_IN_MEMORY_SESSIONS=true
   python main.py
   ```

4. **Test it**
   ```bash
   curl http://localhost:8080/health
   ```

---

## Option 3: With Full Guardrails Configuration

### Prerequisites
- Python 3.10 or newer
- Redis (or use in-memory sessions)

### Steps

1. **Clone and install** (same as Option 2)

2. **Configure guardrails**
   ```bash
   export MCP_BLOCKED_PATTERNS='["rm -rf", "sudo ", "docker rm"]'
   export MCP_PROTECTED_PATHS='["/etc", "/usr", "/var"]'
   export MCP_MAX_RESPONSE_SIZE="2097152"
   export MCP_ALLOW_IN_MEMORY_SESSIONS=true
   ```

3. **Run**
   ```bash
   python main.py
   ```

---

## What Happens When You Run It?

The gateway starts on `http://127.0.0.1:8080` with:

### **Middleware Pipeline Active**
1. **Telemetry** - Logs every request/response
2. **Guardrails** - Blocks dangerous commands
3. **Custom** - Your custom middleware (if any)

### **Guardrails Examples**

❌ **Blocked:**
```json
{"command": "rm -rf /etc"}
{"command": "sudo apt-get install"}
{"path": "/etc/passwd"}
```

✅ **Allowed:**
```json
{"command": "ls -la"}
{"command": "cat file.txt"}
{"path": "/tmp/myfile"}
```

### **Example Logs**

```
2025-01-13 10:23:45 - gateway.telemetry - INFO - REQUEST method=tools/call tool=bash user=test@example.com size=245 bytes
2025-01-13 10:23:45 - gateway.guardrails - WARNING - Tool arguments blocked: bash - Argument matches blocked pattern: rm -rf
2025-01-13 10:23:45 - gateway.telemetry - INFO - RESPONSE method=tools/call tool=bash user=test@example.com latency=2.34ms size=189 bytes status=error
```

---

## Next Steps

### **Connect Claude Desktop**

1. Copy `claude_desktop_config.example.json` to your Claude config:
   ```bash
   # Linux/Mac
   cp claude_desktop_config.example.json ~/.config/Claude/claude_desktop_config.json
   
   # Windows
   copy claude_desktop_config.example.json %APPDATA%\Claude\claude_desktop_config.json
   ```

2. Edit the file to update the path to your installation

3. Restart Claude Desktop

### **Add Custom Middleware**

See `examples/` directory for:
- Rate limiting
- Caching
- Enhanced audit logging

### **Production Deployment**

For production, see:
- `README.md` - Full documentation
- `deploy/k8s/staging.yaml` - Kubernetes deployment
- `Dockerfile` - Docker configuration

---

## Troubleshooting

### **Port already in use?**
```bash
# Change port
export MCP_GATEWAY_PORT=8081
```

### **Redis connection error?**
```bash
# Use in-memory sessions for testing
export MCP_ALLOW_IN_MEMORY_SESSIONS=true
```

### **Guardrails not working?**
```bash
# Verify environment variable
echo $MCP_BLOCKED_PATTERNS
# Should output: ["rm -rf", "sudo ", "docker rm", "kubectl delete"]
```

For more help, see `TROUBLESHOOTING.md`.

---

## Need Help?

- 📖 Full documentation: `README.md`
- 🐛 Issues: https://github.com/shouryasharan7-netizen/mcpmiddleware/issues
- 💬 Discussions: https://github.com/shouryasharan7-netizen/mcpmiddleware/discussions

---

**Ready to secure your MCP deployment? Start with Option 1 (Docker Compose) - it's the easiest!** 🚀
