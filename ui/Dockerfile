FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
COPY wheels/ /tmp/wheels/

RUN python -m pip install --upgrade pip && \
    python -m pip install --no-cache-dir -r /tmp/requirements.txt && \
    if find /tmp/wheels -maxdepth 1 -name '*.whl' -print -quit | grep -q .; then \
      python -m pip install --no-cache-dir /tmp/wheels/*.whl; \
    else \
      python -m pip install --no-cache-dir couchbase-agent-memory; \
    fi

COPY app /app/app
COPY static /app/static
COPY agent_catalog /app/agent_catalog
COPY .agent-catalog /app/.agent-catalog

EXPOSE 8088

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088"]
