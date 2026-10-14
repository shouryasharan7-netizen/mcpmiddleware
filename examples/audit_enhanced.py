"""Example enhanced audit middleware with detailed logging."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict

from pipeline import Middleware, RequestContext, ResponseContext, ErrorContext

logger = logging.getLogger("gateway.audit")


class EnhancedAuditMiddleware(Middleware):
    """
    Enhanced audit middleware that logs detailed request/response information.
    
    Useful for compliance, debugging, and security monitoring.
    """

    def __init__(self, log_level: str = "INFO"):
        self.logger = logger
        self.logger.setLevel(getattr(logging, log_level.upper()))

    async def before_request(self, request, context):
        """Log incoming request details."""
        method = request.get("method", "unknown")
        tool_name = request.get("params", {}).get("name", "N/A") if method == "tools/call" else "N/A"
        
        self.logger.info(
            f"REQUEST: user={context.identity.get('sub')} "
            f"method={method} tool={tool_name} "
            f"session={context.session_id}"
        )
        
        # Log sensitive operations
        if method in ("tools/call", "tools/list"):
            self.logger.debug(f"REQUEST_PARAMS: {json.dumps(request.get('params', {}))}")
        
        return request

    async def after_response(self, response, context):
        """Log response details."""
        method = context.request_context.request.get("method", "unknown")
        tool_name = context.request_context.request.get("params", {}).get("name", "N/A") if method == "tools/call" else "N/A"
        
        is_error = "error" in response
        status = "ERROR" if is_error else "SUCCESS"
        
        self.logger.info(
            f"RESPONSE: user={context.request_context.identity.get('sub')} "
            f"method={method} tool={tool_name} "
            f"status={status}"
        )
        
        if is_error:
            self.logger.warning(
                f"ERROR_RESPONSE: {json.dumps(response.get('error', {}))}"
            )
        
        return response

    async def on_error(self, error, context):
        """Log error details."""
        method = context.request_context.request.get("method", "unknown")
        tool_name = context.request_context.request.get("params", {}).get("name", "N/A") if method == "tools/call" else "N/A"
        
        self.logger.error(
            f"EXCEPTION: user={context.request_context.identity.get('sub')} "
            f"method={method} tool={tool_name} "
            f"error_type={type(error).__name__} "
            f"error_message={str(error)}"
        )
        
        return None
