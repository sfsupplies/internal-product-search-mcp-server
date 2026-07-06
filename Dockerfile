FROM python:3.11-slim

WORKDIR /app

# Copy project and install the package + its runtime deps (from pyproject).
COPY . .
RUN pip install --no-cache-dir .

# HTTP serving defaults (override at runtime).
ENV MCP_TRANSPORT=http \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8000

EXPOSE 8000

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

# Liveness check (no curl in slim image — use Python).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)" || exit 1

CMD ["python", "-m", "product_search_mcp_server"]
