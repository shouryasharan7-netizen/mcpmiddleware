"""Guardrails middleware for tool-call interception and policy enforcement."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

from pipeline import ErrorContext, Middleware, RequestContext, ResponseContext

logger = logging.getLogger("gateway.guardrails")


class GuardrailsConfig:
    """Configuration for guardrails policy."""

    def __init__(self):
        # Load from environment variables
        self.blocked_tools: Set[str] = set(
            json.loads(os.getenv("MCP_BLOCKED_TOOLS", "[]"))
        )
        self.allowed_tools: Set[str] = set(
            json.loads(os.getenv("MCP_ALLOWED_TOOLS", "[]"))
        )
        self.blocked_patterns: List[str] = json.loads(
            os.getenv("MCP_BLOCKED_PATTERNS", '["rm -rf", "sudo ", "docker rm", "kubectl delete"]')
        )
        self.protected_paths: List[str] = json.loads(
            os.getenv("MCP_PROTECTED_PATHS", '["/etc", "/usr", "/var", "/home", "/root"]')
        )
        self.max_response_size: int = int(
            os.getenv("MCP_MAX_RESPONSE_SIZE", str(2 * 1024 * 1024))  # 2MB default
        )
        self.max_tokens: Optional[int] = int(os.getenv("MCP_MAX_TOKENS", "0")) or None

        # Compile regex patterns
        self._compiled_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in self.blocked_patterns]

    def is_tool_blocked(self, tool_name: str) -> bool:
        """Check if a tool is explicitly blocked."""
        if self.blocked_tools and tool_name in self.blocked_tools:
            return True
        # If allowlist is configured, block anything not in it
        if self.allowed_tools and tool_name not in self.allowed_tools:
            return True
        return False

    def check_arguments(self, arguments: Dict[str, Any]) -> tuple[bool, str]:
        """
        Check tool arguments against blocked patterns and protected paths.
        
        Returns:
            Tuple of (is_blocked, reason)
        """
        args_str = json.dumps(arguments, ensure_ascii=False)
        
        # Check for blocked patterns
        for pattern in self._compiled_patterns:
            if pattern.search(args_str):
                return True, f"Argument matches blocked pattern: {pattern.pattern}"
        
        # Check for protected paths in string arguments
        for path in self.protected_paths:
            if path in args_str:
                return True, f"Argument references protected path: {path}"
        
        return False, ""


class GuardrailsMiddleware(Middleware):
    """
    Middleware that enforces guardrails on tool calls.
    
    Features:
    - Tool name blocklist/allowlist
    - Argument pattern matching (e.g., blocking destructive bash commands)
    - Protected path detection
    - Response size limiting
    """

    def __init__(self, config: Optional[GuardrailsConfig] = None):
        self.config = config or GuardrailsConfig()
        logger.info(
            f"Guardrails initialized: {len(self.config.blocked_tools)} blocked tools, "
            f"{len(self.config.allowed_tools)} allowed tools, "
            f"{len(self.config.blocked_patterns)} blocked patterns"
        )

    async def before_request(self, request: Dict[str, Any], context: RequestContext) -> Optional[Dict[str, Any]]:
        """Intercept tool calls and enforce policy."""
        method = request.get("method")
        
        # Only intercept tools/call
        if method != "tools/call":
            return request
        
        params = request.get("params", {})
        if not isinstance(params, dict):
            return request
        
        tool_name = params.get("name")
        if not isinstance(tool_name, str):
            return request
        
        # Check tool name against blocklist/allowlist
        if self.config.is_tool_blocked(tool_name):
            logger.warning(f"Tool blocked by policy: {tool_name}")
            context.metadata["guardrails_blocked"] = True
            context.metadata["guardrails_reason"] = f"Tool '{tool_name}' is blocked by policy"
            return None  # Reject the request
        
        # Check arguments against patterns
        arguments = params.get("arguments", {})
        if isinstance(arguments, dict):
            is_blocked, reason = self.config.check_arguments(arguments)
            if is_blocked:
                logger.warning(f"Tool arguments blocked: {tool_name} - {reason}")
                context.metadata["guardrails_blocked"] = True
                context.metadata["guardrails_reason"] = reason
                return None  # Reject the request
        
        return request

    async def after_response(self, response: Dict[str, Any], context: ResponseContext) -> Dict[str, Any]:
        """Apply response size limits."""
        # Calculate response size
        response_str = json.dumps(response, ensure_ascii=False)
        response_size = len(response_str.encode('utf-8'))
        
        if response_size > self.config.max_response_size:
            logger.warning(
                f"Response size {response_size} exceeds limit {self.config.max_response_size}, "
                f"truncating for tool: {context.request_context.request.get('params', {}).get('name', 'unknown')}"
            )
            
            # Truncate the response
            truncated = self._truncate_response(response, self.config.max_response_size)
            context.metadata["response_truncated"] = True
            context.metadata["original_size"] = response_size
            context.metadata["truncated_size"] = self.config.max_response_size
            return truncated
        
        return response

    async def on_error(self, error: Exception, context: ErrorContext) -> Optional[Dict[str, Any]]:
        """Handle guardrails-related errors."""
        # If the error is due to guardrails, return a proper JSON-RPC error
        if context.request_context.metadata.get("guardrails_blocked"):
            return {
                "jsonrpc": "2.0",
                "id": context.request_context.request.get("id"),
                "error": {
                    "code": -32098,
                    "message": "Tool call blocked by guardrails policy",
                    "data": {
                        "reason": context.request_context.metadata.get("guardrails_reason", "Unknown")
                    }
                }
            }
        return None

    def _truncate_response(self, response: Dict[str, Any], max_size: int) -> Dict[str, Any]:
        """
        Safely truncate a response to fit within size limits.
        
        Truncates the 'result.content' array if present, otherwise truncates string values.
        """
        # Calculate current size
        response_str = json.dumps(response, ensure_ascii=False)
        if len(response_str.encode('utf-8')) <= max_size:
            return response
        
        # If it's a tool response with content array, truncate that
        if "result" in response and isinstance(response["result"], dict):
            result = response["result"]
            
            # Handle structured content
            if "structuredContent" in result:
                structured = result["structuredContent"]
                truncated_structured = self._truncate_value(structured, max_size)
                result["structuredContent"] = truncated_structured
                
                # Update text content to reflect truncation
                if "content" in result and isinstance(result["content"], list):
                    for item in result["content"]:
                        if isinstance(item, dict) and item.get("type") == "text":
                            item["text"] = json.dumps(truncated_structured, ensure_ascii=False)
                            item["text"] += f"\n\n[Response truncated at {max_size} bytes]"
            
            # Recalculate size
            response_str = json.dumps(response, ensure_ascii=False)
            if len(response_str.encode('utf-8')) > max_size:
                # Fallback: truncate the entire response as JSON
                truncated_json = response_str[:max_size]
                return {
                    "jsonrpc": "2.0",
                    "id": response.get("id"),
                    "result": {
                        "content": [{"type": "text", "text": truncated_json + "\n\n[Response truncated]"}],
                        "isError": False
                    }
                }
        
        return response

    def _truncate_value(self, value: Any, max_size: int) -> Any:
        """Recursively truncate a value to fit within size limits."""
        if isinstance(value, str):
            return value[:max_size]
        elif isinstance(value, dict):
            result = {}
            for k, v in value.items():
                result[k] = self._truncate_value(v, max_size)
            return result
        elif isinstance(value, list):
            return [self._truncate_value(item, max_size) for item in value]
        else:
            return value
