FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY vendor/couchbase_mcp_server-1.0.0.post1.tar.gz /tmp/couchbase_mcp_server.tar.gz

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && \
    rm -rf /var/lib/apt/lists/* && \
    python -m pip install --upgrade pip && \
    python -m pip install --no-cache-dir /tmp/couchbase_mcp_server.tar.gz

EXPOSE 8000
ENTRYPOINT ["couchbase-mcp-server"]
