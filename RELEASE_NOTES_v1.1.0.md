# Release v1.1.0 - Middleware Pipeline with Guardrails & Telemetry

## 🎉 Overview

This release introduces a **pluggable middleware pipeline** that transforms the MCP Gateway from a basic proxy into a production-grade, extensible middleware gateway. Add guardrails, telemetry, and custom policies without breaking MCP protocol compliance.

## ✨ New Features

### 1. Pluggable Middleware Pipeline
- **beforeRequest hook** - Intercept and modify/reject requests before they reach upstream servers
- **afterResponse hook** - Transform responses before returning to clients
- **onError hook** - Handle errors with custom logic
- Clean, async architecture with zero overhead
- Easy to extend with custom middleware

### 2. Guardrails Middleware
- **Tool name blocking** - Blocklist/allowlist for specific tools
- **Pattern matching** - Regex-based blocking of dangerous commands (e.g., `rm -rf`, `sudo`)
- **Protected path detection** - Prevent access to sensitive filesystem paths (`/etc`, `/usr`, `/var`)
- **Configurable via environment variables** - No code changes needed
- **Standard JSON-RPC error responses** - Error code `-32098` for policy violations

### 3. Output Truncation
- **Configurable size limits** - Prevent context overflow from large responses
- **Safe truncation** - Preserves JSON-RPC structure
- **Automatic truncation notice** - `[Response truncated at X bytes]`
- **Default: 2MB limit** (configurable via `MCP_MAX_RESPONSE_SIZE`)

### 4. Lightweight Telemetry
- **Request logging** - Method, tool name, user, packet size
- **Response logging** - Latency, size, success/failure status
- **Error logging** - Error type, message, context
- **Logs to stderr** by default (configurable to any logger)
- **Non-blocking async** - No performance impact

### 5. Example Middleware Implementations
- **Rate limiting** - Token bucket algorithm
- **Caching** - In-memory response caching for read operations
- **Enhanced audit** - Detailed logging for compliance

## 📦 Installation

### Quick Start (Docker Compose)
```bash
git clone https://github.com/shouryasharan7-netizen/mcpmiddleware.git
cd mcpmiddleware
docker-compose up -d
```

### Python Installation
```bash
git clone https://github.com/shouryasharan7-netizen/mcpmiddleware.git
cd mcpmiddleware
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MCP_ALLOW_IN_MEMORY_SESSIONS=true
python main.py
```

For detailed installation instructions, see [QUICKSTART.md](QUICKSTART.md).

## ⚙️ Configuration

### Guardrails Configuration
```bash
# Block specific tools
export MCP_BLOCKED_TOOLS='["dangerous_tool", "delete_database"]'

# Allow only specific tools (blocks all others)
export MCP_ALLOWED_TOOLS='["safe_tool", "read_file"]'

# Block dangerous command patterns
export MCP_BLOCKED_PATTERNS='["rm -rf", "sudo ", "docker rm", "kubectl delete"]'

# Protect sensitive paths
export MCP_PROTECTED_PATHS='["/etc", "/usr", "/var", "/home", "/root"]'

# Limit response size
export MCP_MAX_RESPONSE_SIZE="2097152"  # 2MB
```

### Example: Blocking Dangerous Commands
```bash
export MCP_BLOCKED_PATTERNS='["rm -rf", "sudo "]'
python main.py
```

Now when an AI tries to execute `rm -rf /etc`, the gateway returns:
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

## 📚 Documentation

- [README.md](README.md) - Full documentation with architecture diagram
- [QUICKSTART.md](QUICKSTART.md) - 5-minute quick start guide
- [CONTRIBUTING.md](CONTRIBUTING.md) - Development guidelines
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) - Common issues and solutions
- [examples/](examples/) - Custom middleware examples

## 🧪 Testing

Run the test suite:
```bash
python -m unittest test_pipeline -v
```

**Test Coverage:**
- 22 unit tests
- All tests passing ✅
- Covers pipeline, guardrails, telemetry, and truncation

## 🔄 Migration from v1.0.0

**This release is backward compatible** - no breaking changes!

Existing configurations will continue to work. The middleware pipeline is automatically initialized with Telemetry and Guardrails middleware.

To enable guardrails, simply set the environment variables:
```bash
export MCP_BLOCKED_PATTERNS='["rm -rf", "sudo "]'
```

## 🐛 Bug Fixes

- Fixed JSON escaping in default blocked patterns
- Improved error messages for guardrails violations
- Added proper handling of malformed JSON in requests

## 🚀 Performance

- Zero overhead for middleware pipeline (async, non-blocking)
- Telemetry logging is non-blocking
- Pattern matching is pre-compiled at startup
- Memory usage: Minimal (no additional overhead)

## 🔒 Security

- Guardrails provide defense-in-depth for AI systems
- Pattern matching prevents command injection
- Protected path detection prevents unauthorized file access
- Output truncation prevents context overflow attacks
- All security policies enforced before reaching upstream servers

## 📈 What's Next

### Roadmap for v1.2.0
- [ ] Built-in rate limiting middleware
- [ ] Prometheus metrics export
- [ ] Webhook notifications for blocked requests
- [ ] Multi-tenant support
- [ ] Enhanced caching with TTL

### Roadmap for v2.0.0
- [ ] Plugin marketplace
- [ ] Advanced anomaly detection
- [ ] ML-based policy learning
- [ ] GraphQL adapter
- [ ] Event-driven architecture

## 🤝 Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Areas where we need help:
- More middleware examples
- Performance benchmarks
- Documentation improvements
- Bug reports
- Feature requests

## 📝 Changelog

### Added
- Pluggable middleware pipeline (`pipeline.py`)
- Guardrails middleware (`guardrails.py`)
- Telemetry middleware (`telemetry.py`)
- Output truncation feature
- Example middleware implementations
- Comprehensive unit tests (22 tests)
- CI/CD with GitHub Actions
- Docker Compose configuration
- Quick start guide
- Troubleshooting guide
- Contributing guidelines

### Changed
- Updated README with architecture diagram
- Added badges (tests, Python, license, MCP)
- Improved documentation structure

### Fixed
- JSON escaping in default patterns
- Error handling in middleware pipeline
- Session timeout error messages

## 🙏 Acknowledgments

- The MCP community for building such an important protocol
- Contributors who provided feedback and suggestions
- Open source maintainers of dependencies

## 📞 Support

- **Issues**: https://github.com/shouryasharan7-netizen/mcpmiddleware/issues
- **Discussions**: https://github.com/shouryasharan7-netizen/mcpmiddleware/discussions
- **Documentation**: https://github.com/shouryasharan7-netizen/mcpmiddleware/blob/main/README.md

## ⚠️ Important Notes

- **Guardrails are defense-in-depth** - they do not replace backend authorization
- **Pattern matching is regex-based** - test patterns before deploying to production
- **Output truncation may break clients** expecting full responses
- **Telemetry logs may contain sensitive data** - configure log redaction in production
- **Redis is required for production** - use `MCP_ALLOW_IN_MEMORY_SESSIONS=true` only for development

## 📦 Downloads

- **Source**: https://github.com/shouryasharan7-netizen/mcpmiddleware/archive/refs/tags/v1.1.0.tar.gz
- **Docker**: `docker pull shouryasharan7-netizen/mcpmiddleware:v1.1.0` (after CI setup)

---

**Full Release**: https://github.com/shouryasharan7-netizen/mcpmiddleware/releases/tag/v1.1.0

**Generated**: 2025-01-13
