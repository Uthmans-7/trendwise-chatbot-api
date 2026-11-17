# Chat API image: Python 3.12 slim, deps via uv, non-root, uvicorn on :8000.
#
# Contents: pyproject.toml, uv.lock, app/ and corpus/ only (see
# .dockerignore). No .env. The cleaned corpus is baked in at /app/corpus;
# CORPUS_DIR can still point at a mounted copy instead.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    CORPUS_DIR=/app/corpus \
    DOCS_ENABLED=false

WORKDIR /app

# Runtime deps only: no dev tools.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-default-groups

COPY app/ ./app/
COPY corpus/ ./corpus/

RUN useradd --system --uid 10001 --no-create-home app
USER app

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]

# One worker: sessions are in memory (v1). Scale out only with a shared store.
CMD ["uvicorn", "--factory", "app.main:create_app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--no-server-header"]
