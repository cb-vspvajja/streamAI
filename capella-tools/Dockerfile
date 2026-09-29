FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m pip install --no-cache-dir "couchbase>=4.6,<5.0" "httpx>=0.28,<1.0"
WORKDIR /workspace
