"""Example middleware implementations for the MCP gateway.

This directory contains example middleware implementations that demonstrate
how to extend the MCP gateway with custom functionality:
- rate_limit.py: Rate limiting using token bucket algorithm
- caching.py: Simple in-memory caching for read operations
- audit_enhanced.py: Enhanced audit logging with detailed information

To use these examples, import them and register with the pipeline:

    from examples.rate_limit import RateLimitMiddleware
    from pipeline import get_pipeline
    
    pipeline = get_pipeline()
    pipeline.use(RateLimitMiddleware(requests_per_minute=60))
"""
