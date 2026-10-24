# aigateway production image
FROM python:3.12-slim AS base

# Unprivileged runtime user
RUN useradd --create-home --uid 10001 aigw

WORKDIR /app

# Install locked dependencies first for layer caching
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --no-install-project

# Install the application
COPY src/ src/
COPY README.md .
RUN uv sync --frozen --no-dev

USER aigw
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

# Config is mounted at runtime; server.admin_api_key etc. come from it.
CMD ["uv", "run", "--no-sync", "aigateway", "--config", "/etc/aigateway/gateway.yaml", "--host", "0.0.0.0", "--port", "8080"]
