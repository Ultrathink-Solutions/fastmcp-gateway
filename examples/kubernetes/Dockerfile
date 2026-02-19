# Multi-stage build for fastmcp-gateway
#
# Usage:
#   docker build -f examples/kubernetes/Dockerfile .
#   docker run -e GATEWAY_UPSTREAMS='{"svc": "http://svc:8080/mcp"}' -p 8080:8080 fastmcp-gateway

FROM python:3.12-slim AS builder

WORKDIR /build

# Install the package (pulls from PyPI)
RUN pip install --no-cache-dir --prefix=/install fastmcp-gateway

# --- Runtime stage ---
FROM python:3.12-slim

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g appuser -s /bin/bash appuser

# Defaults (override via environment variables or k8s ConfigMap)
ENV GATEWAY_HOST=0.0.0.0
ENV GATEWAY_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

USER 1000:1000

CMD ["python", "-m", "fastmcp_gateway"]
