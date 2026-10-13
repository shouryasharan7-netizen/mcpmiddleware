"""Unit tests for middleware pipeline, guardrails, and telemetry."""
import asyncio
import json
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline import (
    Pipeline,
    RequestContext,
    ResponseContext,
    ErrorContext,
    Middleware,
    PassThroughMiddleware,
    get_pipeline,
    reset_pipeline,
)
from guardrails import GuardrailsMiddleware, GuardrailsConfig
from telemetry import TelemetryMiddleware


class TestMiddleware(Middleware):
    """Test middleware for unit tests."""

    def __init__(self, modify_request=False, modify_response=False, reject=False):
        self.modify_request = modify_request
        self.modify_response = modify_response
        self.reject = reject
        self.before_called = False
        self.after_called = False
        self.error_called = False

    async def before_request(self, request, context):
        self.before_called = True
        if self.reject:
            return None
        if self.modify_request:
            modified = dict(request)
            modified["test_modified"] = True
            return modified
        return request

    async def after_response(self, response, context):
        self.after_called = True
        if self.modify_response:
            modified = dict(response)
            modified["test_modified"] = True
            return modified
        return response

    async def on_error(self, error, context):
        self.error_called = True
        return None


class TestPipeline(unittest.TestCase):
    """Test cases for Pipeline class."""

    def setUp(self):
        """Reset pipeline before each test."""
        reset_pipeline()

    def test_empty_pipeline(self):
        """Test that empty pipeline passes requests through unchanged."""
        pipeline = Pipeline()
        request = {"jsonrpc": "2.0", "method": "test"}
        context = RequestContext(
            request=request,
            identity={"sub": "test"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )

        async def run_test():
            processed, error = await pipeline.process_request(request, context)
            self.assertEqual(processed, request)
            self.assertIsNone(error)

        asyncio.run(run_test())

    def test_middleware_registration(self):
        """Test that middleware can be registered."""
        pipeline = Pipeline()
        middleware = TestMiddleware()
        pipeline.use(middleware)
        self.assertEqual(len(pipeline._middleware), 1)

    def test_before_request_chain(self):
        """Test that before_request hooks execute in order."""
        pipeline = Pipeline()
        m1 = TestMiddleware(modify_request=True)
        m2 = TestMiddleware(modify_request=True)
        pipeline.use(m1)
        pipeline.use(m2)

        request = {"jsonrpc": "2.0", "method": "test"}
        context = RequestContext(
            request=request,
            identity={"sub": "test"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )

        async def run_test():
            processed, error = await pipeline.process_request(request, context)
            self.assertTrue(m1.before_called)
            self.assertTrue(m2.before_called)
            self.assertIsNotNone(processed)
            self.assertIn("test_modified", processed)

        asyncio.run(run_test())

    def test_request_rejection(self):
        """Test that middleware can reject requests."""
        pipeline = Pipeline()
        middleware = TestMiddleware(reject=True)
        pipeline.use(middleware)

        request = {"jsonrpc": "2.0", "id": 1, "method": "test"}
        context = RequestContext(
            request=request,
            identity={"sub": "test"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )

        async def run_test():
            processed, error = await pipeline.process_request(request, context)
            self.assertIsNone(processed)
            self.assertIsNotNone(error)
            self.assertEqual(error["jsonrpc"], "2.0")
            self.assertEqual(error["id"], 1)
            self.assertIn("error", error)

        asyncio.run(run_test())

    def test_after_response_reverse_order(self):
        """Test that after_response hooks execute in reverse order."""
        pipeline = Pipeline()
        m1 = TestMiddleware(modify_response=True)
        m2 = TestMiddleware(modify_response=True)
        pipeline.use(m1)
        pipeline.use(m2)

        request = {"jsonrpc": "2.0", "method": "test"}
        request_context = RequestContext(
            request=request,
            identity={"sub": "test"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )
        response = {"result": "test"}
        response_context = ResponseContext(response=response, request_context=request_context)

        async def run_test():
            processed = await pipeline.process_response(response, response_context)
            self.assertTrue(m1.after_called)
            self.assertTrue(m2.after_called)
            self.assertIn("test_modified", processed)

        asyncio.run(run_test())

    def test_on_error_handling(self):
        """Test that on_error hooks can return error responses."""
        pipeline = Pipeline()
        middleware = TestMiddleware()
        pipeline.use(middleware)

        request = {"jsonrpc": "2.0", "method": "test"}
        request_context = RequestContext(
            request=request,
            identity={"sub": "test"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )
        error = Exception("Test error")
        error_context = ErrorContext(error=error, request_context=request_context)

        async def run_test():
            result = await pipeline.process_error(error, error_context)
            self.assertTrue(middleware.error_called)
            self.assertIsNone(result)  # TestMiddleware returns None

        asyncio.run(run_test())

    def test_global_pipeline(self):
        """Test global pipeline singleton."""
        pipeline1 = get_pipeline()
        pipeline2 = get_pipeline()
        self.assertIs(pipeline1, pipeline2)
        reset_pipeline()
        pipeline3 = get_pipeline()
        self.assertIsNot(pipeline1, pipeline3)


class TestGuardrailsConfig(unittest.TestCase):
    """Test cases for GuardrailsConfig."""

    def setUp(self):
        """Reset environment before each test."""
        self.original_env = dict(os.environ)
        os.environ.clear()

    def tearDown(self):
        """Restore environment after each test."""
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_default_config(self):
        """Test default configuration values."""
        config = GuardrailsConfig()
        self.assertEqual(config.blocked_tools, set())
        self.assertEqual(config.allowed_tools, set())
        self.assertTrue(len(config.blocked_patterns) > 0)
        self.assertTrue(len(config.protected_paths) > 0)
        self.assertEqual(config.max_response_size, 2 * 1024 * 1024)

    def test_custom_blocked_tools(self):
        """Test custom blocked tools from environment."""
        os.environ["MCP_BLOCKED_TOOLS"] = '["dangerous_tool", "rm_file"]'
        config = GuardrailsConfig()
        self.assertIn("dangerous_tool", config.blocked_tools)
        self.assertIn("rm_file", config.blocked_tools)

    def test_custom_allowed_tools(self):
        """Test custom allowed tools from environment."""
        os.environ["MCP_ALLOWED_TOOLS"] = '["safe_tool", "read_file"]'
        config = GuardrailsConfig()
        self.assertIn("safe_tool", config.allowed_tools)
        self.assertIn("read_file", config.allowed_tools)

    def test_is_tool_blocked_blocklist(self):
        """Test tool blocking with blocklist."""
        os.environ["MCP_BLOCKED_TOOLS"] = '["dangerous_tool"]'
        config = GuardrailsConfig()
        self.assertTrue(config.is_tool_blocked("dangerous_tool"))
        self.assertFalse(config.is_tool_blocked("safe_tool"))

    def test_is_tool_blocked_allowlist(self):
        """Test tool blocking with allowlist."""
        os.environ["MCP_ALLOWED_TOOLS"] = '["safe_tool"]'
        config = GuardrailsConfig()
        self.assertTrue(config.is_tool_blocked("dangerous_tool"))
        self.assertFalse(config.is_tool_blocked("safe_tool"))

    def test_check_arguments_patterns(self):
        """Test argument pattern matching."""
        os.environ["MCP_BLOCKED_PATTERNS"] = '["rm -rf", "sudo "]'
        config = GuardrailsConfig()
        
        is_blocked, reason = config.check_arguments({"command": "rm -rf /etc"})
        self.assertTrue(is_blocked)
        self.assertIn("rm -rf", reason)
        
        is_blocked, reason = config.check_arguments({"command": "ls -la"})
        self.assertFalse(is_blocked)
        
        is_blocked, reason = config.check_arguments({"command": "sudo apt-get install"})
        self.assertTrue(is_blocked)

    def test_check_arguments_protected_paths(self):
        """Test protected path detection."""
        os.environ["MCP_PROTECTED_PATHS"] = '["/etc", "/var"]'
        config = GuardrailsConfig()
        
        is_blocked, reason = config.check_arguments({"path": "/etc/passwd"})
        self.assertTrue(is_blocked)
        self.assertIn("/etc", reason)
        
        is_blocked, reason = config.check_arguments({"path": "/tmp/file"})
        self.assertFalse(is_blocked)


class TestGuardrailsMiddleware(unittest.TestCase):
    """Test cases for GuardrailsMiddleware."""

    def setUp(self):
        """Reset environment and pipeline before each test."""
        self.original_env = dict(os.environ)
        os.environ.clear()
        reset_pipeline()

    def tearDown(self):
        """Restore environment after each test."""
        os.environ.clear()
        os.environ.update(self.original_env)
        reset_pipeline()

    def test_before_request_passthrough_non_tool_call(self):
        """Test that non-tools/call requests pass through."""
        middleware = GuardrailsMiddleware()
        request = {"jsonrpc": "2.0", "method": "initialize", "params": {}}
        context = RequestContext(
            request=request,
            identity={"sub": "test"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )

        async def run_test():
            result = await middleware.before_request(request, context)
            self.assertEqual(result, request)

        asyncio.run(run_test())

    def test_before_request_blocked_tool(self):
        """Test that blocked tools are rejected."""
        os.environ["MCP_BLOCKED_TOOLS"] = '["dangerous_tool"]'
        middleware = GuardrailsMiddleware()
        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "dangerous_tool", "arguments": {}},
        }
        context = RequestContext(
            request=request,
            identity={"sub": "test"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )

        async def run_test():
            result = await middleware.before_request(request, context)
            self.assertIsNone(result)
            self.assertTrue(context.metadata.get("guardrails_blocked"))
            self.assertIn("dangerous_tool", context.metadata.get("guardrails_reason", ""))

        asyncio.run(run_test())

    def test_before_request_blocked_arguments(self):
        """Test that arguments matching blocked patterns are rejected."""
        os.environ["MCP_BLOCKED_PATTERNS"] = '["rm -rf"]'
        middleware = GuardrailsMiddleware()
        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "bash", "arguments": {"command": "rm -rf /tmp"}},
        }
        context = RequestContext(
            request=request,
            identity={"sub": "test"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )

        async def run_test():
            result = await middleware.before_request(request, context)
            self.assertIsNone(result)
            self.assertTrue(context.metadata.get("guardrails_blocked"))

        asyncio.run(run_test())

    def test_after_response_truncation(self):
        """Test that oversized responses are truncated."""
        os.environ["MCP_MAX_RESPONSE_SIZE"] = "100"
        middleware = GuardrailsMiddleware()
        
        request = {"jsonrpc": "2.0", "method": "tools/call"}
        request_context = RequestContext(
            request=request,
            identity={"sub": "test"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )
        
        # Create a large response
        large_content = "x" * 1000
        response = {
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": large_content}],
                "structuredContent": {"data": large_content}
            }
        }
        response_context = ResponseContext(response=response, request_context=request_context)

        async def run_test():
            result = await middleware.after_response(response, response_context)
            self.assertTrue(response_context.metadata.get("response_truncated"))
            self.assertLess(len(json.dumps(result)), len(json.dumps(response)))

        asyncio.run(run_test())

    def test_on_error_guardrails_blocked(self):
        """Test that guardrails rejection errors return proper JSON-RPC error."""
        middleware = GuardrailsMiddleware()
        
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
        request_context = RequestContext(
            request=request,
            identity={"sub": "test"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )
        request_context.metadata["guardrails_blocked"] = True
        request_context.metadata["guardrails_reason"] = "Tool blocked by policy"
        
        error = Exception("Test error")
        error_context = ErrorContext(error=error, request_context=request_context)

        async def run_test():
            result = await middleware.on_error(error, error_context)
            self.assertIsNotNone(result)
            self.assertEqual(result["jsonrpc"], "2.0")
            self.assertEqual(result["id"], 1)
            self.assertIn("error", result)
            self.assertEqual(result["error"]["code"], -32098)

        asyncio.run(run_test())


class TestTelemetryMiddleware(unittest.TestCase):
    """Test cases for TelemetryMiddleware."""

    def setUp(self):
        """Reset pipeline before each test."""
        reset_pipeline()

    def tearDown(self):
        """Reset pipeline after each test."""
        reset_pipeline()

    @patch("sys.stderr")
    def test_before_request_logging(self, mock_stderr):
        """Test that before_request logs request details."""
        middleware = TelemetryMiddleware()
        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "test_tool"},
        }
        context = RequestContext(
            request=request,
            identity={"sub": "test-user"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )

        async def run_test():
            result = await middleware.before_request(request, context)
            self.assertEqual(result, request)
            self.assertTrue(context.metadata.get("request_logged"))
            self.assertIn("request_size", context.metadata)

        asyncio.run(run_test())

    @patch("sys.stderr")
    def test_after_response_logging(self, mock_stderr):
        """Test that after_response logs response details."""
        middleware = TelemetryMiddleware()
        
        request = {"jsonrpc": "2.0", "method": "tools/call"}
        request_context = RequestContext(
            request=request,
            identity={"sub": "test-user"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )
        request_context.metadata["request_logged"] = True
        
        response = {"jsonrpc": "2.0", "result": {"content": "test"}}
        response_context = ResponseContext(response=response, request_context=request_context)

        async def run_test():
            result = await middleware.after_response(response, response_context)
            self.assertEqual(result, response)
            self.assertIn("response_size", response_context.metadata)
            self.assertIn("latency_ms", response_context.metadata)

        asyncio.run(run_test())

    @patch("sys.stderr")
    def test_on_error_logging(self, mock_stderr):
        """Test that on_error logs error details."""
        middleware = TelemetryMiddleware()
        
        request = {"jsonrpc": "2.0", "method": "tools/call"}
        request_context = RequestContext(
            request=request,
            identity={"sub": "test-user"},
            session_id="session-123",
            start_time=time.perf_counter(),
        )
        request_context.metadata["request_logged"] = True
        
        error = ValueError("Test error")
        error_context = ErrorContext(error=error, request_context=request_context)

        async def run_test():
            result = await middleware.on_error(error, error_context)
            self.assertIsNone(result)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
