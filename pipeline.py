"""Pluggable middleware pipeline for MCP JSON-RPC request/response interception."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("gateway.pipeline")


@dataclass
class RequestContext:
    """Context object passed through the middleware pipeline."""
    request: Dict[str, Any]
    identity: Dict[str, str]
    session_id: Optional[str]
    start_time: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResponseContext:
    """Context object for response handling."""
    response: Dict[str, Any]
    request_context: RequestContext
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ErrorContext:
    """Context object for error handling."""
    error: Exception
    request_context: RequestContext
    metadata: Dict[str, Any] = field(default_factory=dict)


class Middleware(ABC):
    """Base class for middleware components."""

    @abstractmethod
    async def before_request(self, request: Dict[str, Any], context: RequestContext) -> Optional[Dict[str, Any]]:
        """
        Intercept incoming JSON-RPC request before forwarding to upstream.
        
        Args:
            request: The JSON-RPC request dict
            context: RequestContext with identity, session, etc.
            
        Returns:
            Modified request dict, or None to reject with error.
            If None is returned, the pipeline should return a JSON-RPC error.
        """
        pass

    @abstractmethod
    async def after_response(self, response: Dict[str, Any], context: ResponseContext) -> Dict[str, Any]:
        """
        Intercept JSON-RPC response before returning to client.
        
        Args:
            response: The JSON-RPC response dict
            context: ResponseContext with request context
            
        Returns:
            Modified response dict
        """
        pass

    @abstractmethod
    async def on_error(self, error: Exception, context: ErrorContext) -> Optional[Dict[str, Any]]:
        """
        Handle errors that occur during request processing.
        
        Args:
            error: The exception that occurred
            context: ErrorContext with request context
            
        Returns:
            Optional JSON-RPC error response dict, or None to let default error handling proceed
        """
        pass


class PassThroughMiddleware(Middleware):
    """No-op middleware that passes everything through unchanged."""

    async def before_request(self, request: Dict[str, Any], context: RequestContext) -> Optional[Dict[str, Any]]:
        return request

    async def after_response(self, response: Dict[str, Any], context: ResponseContext) -> Dict[str, Any]:
        return response

    async def on_error(self, error: Exception, context: ErrorContext) -> Optional[Dict[str, Any]]:
        return None


class Pipeline:
    """
    Middleware pipeline that chains middleware components.
    
    Requests flow through before_request hooks in registration order.
    Responses flow through after_response hooks in reverse order.
    """

    def __init__(self):
        self._middleware: List[Middleware] = []

    def use(self, middleware: Middleware) -> "Pipeline":
        """Register a middleware component."""
        self._middleware.append(middleware)
        logger.info(f"Registered middleware: {middleware.__class__.__name__}")
        return self

    def clear(self) -> "Pipeline":
        """Remove all middleware components."""
        self._middleware.clear()
        return self

    async def process_request(self, request: Dict[str, Any], context: RequestContext) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Process a request through the before_request chain.
        
        Returns:
            Tuple of (modified_request, error_response).
            If middleware rejects, error_response will be set and modified_request will be None.
        """
        current_request = request
        for middleware in self._middleware:
            try:
                result = await middleware.before_request(current_request, context)
                if result is None:
                    # Middleware rejected the request
                    error = {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "error": {
                            "code": -32099,
                            "message": f"Request rejected by {middleware.__class__.__name__}"
                        }
                    }
                    return None, error
                current_request = result
            except Exception as exc:
                logger.error(f"Error in {middleware.__class__.__name__}.before_request: {exc}")
                error = {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {
                        "code": -32603,
                        "message": f"Middleware error: {type(exc).__name__}"
                    }
                }
                return None, error
        return current_request, None

    async def process_response(self, response: Dict[str, Any], context: ResponseContext) -> Dict[str, Any]:
        """Process a response through the after_response chain (reverse order)."""
        current_response = response
        for middleware in reversed(self._middleware):
            try:
                current_response = await middleware.after_response(current_response, context)
            except Exception as exc:
                logger.error(f"Error in {middleware.__class__.__name__}.after_response: {exc}")
                # Continue processing even if one middleware fails
        return current_response

    async def process_error(self, error: Exception, context: ErrorContext) -> Optional[Dict[str, Any]]:
        """Process an error through the on_error chain."""
        for middleware in self._middleware:
            try:
                error_response = await middleware.on_error(error, context)
                if error_response is not None:
                    return error_response
            except Exception as exc:
                logger.error(f"Error in {middleware.__class__.__name__}.on_error: {exc}")
        return None


# Global pipeline instance
_global_pipeline: Optional[Pipeline] = None


def get_pipeline() -> Pipeline:
    """Get or create the global middleware pipeline."""
    global _global_pipeline
    if _global_pipeline is None:
        _global_pipeline = Pipeline()
    return _global_pipeline


def reset_pipeline() -> None:
    """Reset the global pipeline (useful for testing)."""
    global _global_pipeline
    _global_pipeline = None
