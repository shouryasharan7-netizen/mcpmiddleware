FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_GATEWAY_HOST=0.0.0.0 \
    MCP_GATEWAY_PORT=8080 \
    MCP_GATEWAY_DB=/var/lib/mcp/gateway.sqlite3 \
    MCP_GATEWAY_AUDIT=/var/lib/mcp/audit.jsonl

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /var/lib/mcp \
    && chown -R 10001:10001 /var/lib/mcp

COPY main.py adapters.py security.py ./
USER 10001:10001

EXPOSE 8080
ENTRYPOINT ["python", "main.py"]
