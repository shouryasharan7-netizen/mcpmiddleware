"""Example rate limiting middleware for the MCP gateway."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Dict

from pipeline import Middleware, RequestContext, ResponseContext, ErrorContext


class RateLimitMiddleware(Middleware):
    """
    Rate limiting middleware using token bucket algorithm.
    
    Limits requests per user/session to prevent abuse.
    """

    def __init__(self, requests_per_minute: int = 60):
        self.requests_per_minute = requests_per_minute
        self.requests: Dict[str, list] = defaultdict(list)
        self.window_seconds = 60

    async def before_request(self, request, context):
        """Check if user has exceeded rate limit."""
        user = context.identity.get("sub", "anonymous")
        current_time = time.time()
        
        # Clean old requests outside the window
        self.requests[user] = [
            ts for ts in self.requests[user]
            if current_time - ts < self.window_seconds
        ]
        
        # Check if limit exceeded
        if len(self.requests[user]) >= self.requests_per_minute:
            context.metadata["rate_limited"] = True
            context.metadata["rate_limit"] = self.requests_per_minute
            return None  # Reject request
        
        # Record this request
        self.requests[user].append(current_time)
        return request

    async def after_response(self, response, context):
        """Add rate limit headers to response."""
        user = context.identity.get("sub", "anonymous")
        remaining = self.requests_per_minute - len(self.requests.get(user, []))
        
        # In a real implementation, you'd add these to HTTP headers
        context.metadata["rate_limit_remaining"] = remaining
        return response

    async def on_error(self, error, context):
        """Handle rate limit errors."""
        if context.metadata.get("rate_limited"):
            return {
                "jsonrpc": "2.0",
                "id": context.request_context.request.get("id"),
                "error": {
                    "code": -32097,
                    "message": "Rate limit exceeded",
                    "data": {
                        "limit": context.metadata.get("rate_limit"),
                        "retry_after": 60
                    }
                }
            }
        return None
