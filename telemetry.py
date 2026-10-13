"""Lightweight telemetry middleware for logging request/response metrics."""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict, Optional

from pipeline import ErrorContext, Middleware, RequestContext, ResponseContext

logger = logging.getLogger("gateway.telemetry")


class TelemetryMiddleware(Middleware):
    """
    Middleware that logs lightweight telemetry for each request.
    
    Logs to stderr by default (configurable via environment).
    Captures:
    - Tool execution latency
    - Method names
    - Packet byte sizes
    - Success/failure status
    """

    def __init__(self):
        self._logger = logging.getLogger("gateway.telemetry")
        # Configure stderr logging if not already configured
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)

    async def before_request(self, request: Dict[str, Any], context: RequestContext) -> Optional[Dict[str, Any]]:
        """Log incoming request details."""
        method = request.get("method", "unknown")
        tool_name = request.get("params", {}).get("name", "N/A") if method == "tools/call" else "N/A"
        
        request_size = len(json.dumps(request, ensure_ascii=False).encode('utf-8'))
        
        self._logger.info(
            f"REQUEST method={method} tool={tool_name} "
            f"user={context.identity.get('sub', 'unknown')} "
            f"size={request_size} bytes"
        )
        
        context.metadata["request_size"] = request_size
        context.metadata["request_logged"] = True
        
        return request

    async def after_response(self, response: Dict[str, Any], context: ResponseContext) -> Dict[str, Any]:
        """Log response details and calculate latency."""
        if not context.request_context.metadata.get("request_logged"):
            return response
        
        elapsed_ms = (time.time() - context.request_context.start_time) * 1000
        method = context.request_context.request.get("method", "unknown")
        tool_name = context.request_context.request.get("params", {}).get("name", "N/A") if method == "tools/call" else "N/A"
        
        response_size = len(json.dumps(response, ensure_ascii=False).encode('utf-8'))
        
        # Determine success/failure
        is_error = "error" in response
        status = "error" if is_error else "success"
        
        self._logger.info(
            f"RESPONSE method={method} tool={tool_name} "
            f"user={context.request_context.identity.get('sub', 'unknown')} "
            f"latency={elapsed_ms:.2f}ms "
            f"size={response_size} bytes "
            f"status={status}"
        )
        
        context.metadata["response_size"] = response_size
        context.metadata["latency_ms"] = elapsed_ms
        
        return response

    async def on_error(self, error: Exception, context: ErrorContext) -> Optional[Dict[str, Any]]:
        """Log error details."""
        if not context.request_context.metadata.get("request_logged"):
            return None
        
        elapsed_ms = (time.time() - context.request_context.start_time) * 1000
        method = context.request_context.request.get("method", "unknown")
        tool_name = context.request_context.request.get("params", {}).get("name", "N/A") if method == "tools/call" else "N/A"
        
        self._logger.error(
            f"ERROR method={method} tool={tool_name} "
            f"user={context.request_context.identity.get('sub', 'unknown')} "
            f"latency={elapsed_ms:.2f}ms "
            f"error_type={type(error).__name__} "
            f"error_message={str(error)}"
        )
        
        return None
