# Contributing to MCP Middleware Gateway

Thank you for your interest in contributing! This document provides guidelines for contributing to the project.

## Development Setup

1. **Fork and clone the repository**
   ```bash
   git clone https://github.com/your-username/mcpmiddleware.git
   cd mcpmiddleware
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run tests**
   ```bash
   python -m unittest test_pipeline -v
   ```

## Code Style

- Follow PEP 8 style guidelines
- Use type hints where appropriate
- Add docstrings to all public functions and classes
- Keep functions focused and under 50 lines when possible
- Use descriptive variable names

## Adding New Middleware

To add a new middleware component:

1. Create a new file in the project root (e.g., `my_middleware.py`)
2. Implement the `Middleware` interface from `pipeline.py`
3. Add unit tests in `test_pipeline.py`
4. Register the middleware in `main.py` startup function
5. Update README.md with configuration options

Example:
```python
from pipeline import Middleware, RequestContext, ResponseContext, ErrorContext

class MyMiddleware(Middleware):
    async def before_request(self, request, context):
        # Your logic here
        return request
    
    async def after_response(self, response, context):
        # Your logic here
        return response
    
    async def on_error(self, error, context):
        # Your logic here
        return None
```

## Testing

- Write unit tests for all new features
- Ensure all tests pass before submitting a PR
- Test on Python 3.10, 3.11, and 3.12
- Use mocking for external dependencies

## Submitting Changes

1. **Create a feature branch**
   ```bash
   git checkout -b feature/my-feature
   ```

2. **Commit your changes**
   ```bash
   git add .
   git commit -m "Add my feature"
   ```

3. **Push to your fork**
   ```bash
   git push origin feature/my-feature
   ```

4. **Create a Pull Request**
   - Describe your changes clearly
   - Reference any related issues
   - Ensure CI checks pass

## Project Structure

```
mcpmiddleware/
├── main.py              # Main application entry point
├── pipeline.py          # Middleware pipeline framework
├── guardrails.py        # Guardrails middleware
├── telemetry.py        # Telemetry middleware
├── security.py          # DLP redaction
├── adapters.py          # REST/gRPC adapters
├── test_pipeline.py     # Unit tests
├── requirements.txt     # Python dependencies
├── Dockerfile          # Docker configuration
├── deploy/              # Kubernetes manifests
└── work/                # E2E test utilities
```

## Code Review Process

1. All PRs must pass CI checks
2. At least one maintainer approval required
3. Code should be reviewed within 48 hours
4. Address all review comments before merging

## Questions?

Feel free to open an issue for questions or discussion.
