"""Example caching middleware for the MCP gateway."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from pipeline import Middleware, RequestContext, ResponseContext, ErrorContext


class CachingMiddleware(Middleware):
    """
    Simple in-memory caching middleware for GET-like operations.
    
    Caches responses based on request hash to reduce backend load.
    """

    def __init__(self, ttl_seconds: int = 300):
        self.ttl_seconds = ttl_seconds
        self.cache: Dict[str, tuple] = {}  # {hash: (response, timestamp)}

    def _hash_request(self, request: Dict[str, Any]) -> str:
        """Create a hash of the request for cache key."""
        # Only cache read operations (tools/list, initialize)
        if request.get("method") not in ("tools/list", "initialize"):
            return None
        
        key = json.dumps(request, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(key.encode()).hexdigest()

    async def before_request(self, request, context):
        """Check cache for existing response."""
        cache_key = self._hash_request(request)
        if not cache_key:
            return request
        
        if cache_key in self.cache:
            response, timestamp = self.cache[cache_key]
            import time
            if time.time() - timestamp < self.ttl_seconds:
                context.metadata["cache_hit"] = True
                context.metadata["cached_response"] = response
                # Return None to skip downstream processing
                # The main handler needs to check for cached_response
            else:
                # Cache expired
                del self.cache[cache_key]
        
        return request

    async def after_response(self, response, context):
        """Cache successful responses."""
        cache_key = self._hash_request(context.request_context.request)
        if not cache_key:
            return response
        
        # Only cache successful responses
        if "error" not in response:
            import time
            self.cache[cache_key] = (response, time.time())
            context.metadata["cache_written"] = True
        
        return response

    async def on_error(self, error, context):
        """Handle errors (no caching for errors)."""
        return None
