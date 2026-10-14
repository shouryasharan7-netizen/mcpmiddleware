# Troubleshooting Guide

This guide covers common issues and their solutions when running the MCP Middleware Gateway.

## Table of Contents

- [Installation Issues](#installation-issues)
- [Configuration Issues](#configuration-issues)
- [Runtime Issues](#runtime-issues)
- [Middleware Issues](#middleware-issues)
- [Performance Issues](#performance-issues)
- [Security Issues](#security-issues)

## Installation Issues

### Python Version Not Supported

**Problem:** `ModuleNotFoundError` or syntax errors after installation.

**Solution:** Ensure you're using Python 3.10 or newer:
```bash
python --version  # Should be 3.10+
```

If not, install a newer version or use pyenv/conda.

### Dependencies Fail to Install

**Problem:** `pip install -r requirements.txt` fails with errors.

**Solution:**
1. Upgrade pip: `pip install --upgrade pip`
2. Install OpenSSL development packages (Linux):
   ```bash
   sudo apt-get install libssl-dev  # Ubuntu/Debian
   sudo yum install openssl-devel  # CentOS/RHEL
   ```
3. Try installing from wheels: `pip install --only-binary :all: -r requirements.txt`

## Configuration Issues

### Redis Connection Refused

**Problem:** `RuntimeError: MCP_REDIS_URL is required`

**Solution:**
1. Start Redis: `docker run -d -p 6379:6379 redis`
2. Or use in-memory sessions for local development:
   ```bash
   export MCP_ALLOW_IN_MEMORY_SESSIONS=true
   ```

### OIDC Authentication Fails

**Problem:** `HTTPUnauthorized: invalid access token`

**Solution:**
1. Verify environment variables are set:
   ```bash
   echo $MCP_OIDC_ISSUER
   echo $MCP_OIDC_AUDIENCE
   echo $MCP_OIDC_JWKS_URI
   ```
2. Check JWKS URI is accessible: `curl $MCP_OIDC_JWKS_URI`
3. Verify token has required claims: `exp`, `sub`, `iss`, `aud`
4. Use JWT debugger to decode and inspect your token

### Guardrails Not Blocking

**Problem:** Dangerous commands still pass through.

**Solution:**
1. Verify environment variable is set correctly:
   ```bash
   echo $MCP_BLOCKED_PATTERNS
   ```
2. Must be valid JSON array:
   ```bash
   export MCP_BLOCKED_PATTERNS='["rm -rf", "sudo "]'
   ```
3. Check logs for guardrails activity:
   ```
   grep "guardrails" /path/to/logs
   ```
4. Ensure middleware is registered in `main.py` startup

## Runtime Issues

### Port Already in Use

**Problem:** `OSError: [Errno 48] Address already in use`

**Solution:**
1. Find process using port 8080:
   ```bash
   lsof -i :8080  # macOS/Linux
   netstat -ano | findstr :8080  # Windows
   ```
2. Kill the process or use a different port:
   ```bash
   export MCP_GATEWAY_PORT=8081
   ```

### SQLite Database Locked

**Problem:** `sqlite3.OperationalError: database is locked`

**Solution:**
1. Check for other processes accessing the database:
   ```bash
   lsof gateway.sqlite3
   ```
2. Ensure only one gateway instance is running
3. Check file permissions: `ls -la gateway.sqlite3`

### Session Timeout Errors

**Problem:** `HTTPNotFound: unknown or expired MCP session`

**Solution:**
1. Increase session TTL:
   ```bash
   export MCP_SESSION_TTL_SECONDS=7200  # 2 hours
   ```
2. Check Redis connectivity: `redis-cli ping`
3. Verify session ID is being passed in headers

## Middleware Issues

### Custom Middleware Not Executing

**Problem:** Custom middleware hooks aren't being called.

**Solution:**
1. Verify middleware is registered:
   ```python
   from pipeline import get_pipeline
   pipeline = get_pipeline()
   pipeline.use(MyMiddleware())
   ```
2. Check registration happens in `startup()` function
3. Add logging to verify execution:
   ```python
   async def before_request(self, request, context):
       print("Middleware executed")
       return request
   ```

### Response Truncation Not Working

**Problem:** Large responses not being truncated.

**Solution:**
1. Verify size limit is set:
   ```bash
   echo $MCP_MAX_RESPONSE_SIZE
   ```
2. Response must be JSON (not SSE streaming)
3. Check logs for truncation messages:
   ```
   grep "truncated" /path/to/logs
   ```

### Pattern Matching Too Aggressive

**Problem:** Legitimate requests being blocked.

**Solution:**
1. Review your patterns:
   ```bash
   echo $MCP_BLOCKED_PATTERNS
   ```
2. Use more specific patterns:
   ```bash
   # Instead of: ["rm"]
   # Use: ["rm -rf", "rm -r"]
   ```
3. Test patterns with regex tester before deploying

## Performance Issues

### High Memory Usage

**Problem:** Gateway consuming excessive memory.

**Solution:**
1. Check session count in Redis:
   ```bash
   redis-cli keys "mcp:session:*" | wc -l
   ```
2. Reduce session TTL
3. Limit concurrent sessions
4. Check for memory leaks in custom middleware

### Slow Response Times

**Problem:** Requests taking too long.

**Solution:**
1. Enable telemetry to identify bottlenecks
2. Check network latency to backend MCP servers
3. Profile with `cProfile`:
   ```bash
   python -m cProfile -o profile.stats main.py
   python -m pstats profile.stats
   ```
4. Consider adding caching middleware

### Database Write Contention

**Problem:** SQLite write operations blocking.

**Solution:**
1. Use WAL mode:
   ```python
   conn.execute("PRAGMA journal_mode=WAL")
   ```
2. Consider migrating to PostgreSQL for production
3. Reduce audit frequency
4. Use connection pooling

## Security Issues

### Token Exposure in Logs

**Problem:** Sensitive tokens appearing in logs.

**Solution:**
1. Reduce log level:
   ```bash
   export LOG_LEVEL=WARNING
   ```
2. Ensure DLP is enabled (default)
3. Configure log aggregation to redact secrets
4. Use structured logging with field redaction

### CORS Errors

**Problem:** Browser requests blocked by CORS policy.

**Solution:**
1. The gateway is designed for server-to-server communication
2. For browser access, add CORS middleware or use a reverse proxy
3. Example nginx configuration:
   ```nginx
   add_header 'Access-Control-Allow-Origin' '*';
   add_header 'Access-Control-Allow-Methods' 'POST, GET, OPTIONS';
   ```

### Unauthorized Tool Access

**Problem:** Users can access tools they shouldn't.

**Solution:**
1. Verify role mapping:
   ```bash
   echo $MCP_ROLE_MAP
   ```
2. Check tool registration has correct roles:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/tools
   ```
3. Ensure OIDC token contains expected groups/roles
4. Review audit logs for unauthorized access attempts

## Getting Help

If you're still experiencing issues:

1. **Check the logs:** Look for error messages and stack traces
2. **Search existing issues:** github.com/shouryasharan7-netizen/mcpmiddleware/issues
3. **Create a minimal reproduction:** Simplify your configuration to isolate the problem
4. **Include diagnostic information:**
   - Python version
   - OS version
   - Relevant environment variables (redact secrets)
   - Error messages and stack traces
   - Steps to reproduce

## Debug Mode

Enable debug logging for troubleshooting:

```bash
export LOG_LEVEL=DEBUG
python main.py
```

This will output detailed information about:
- Request processing
- Middleware execution
- Database operations
- Network calls
- Error conditions
