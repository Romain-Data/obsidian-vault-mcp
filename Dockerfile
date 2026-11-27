# Serveur MCP « coffre » — image multi-architecture (amd64, arm64 : testée sur Raspberry Pi 5)
FROM python:3.12-slim

# ripgrep : recherche rapide dans les notes
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "coffre_mcp.server:creer_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--no-server-header", "--proxy-headers", "--forwarded-allow-ips=*"]
