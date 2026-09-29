FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN python -m pip install --no-cache-dir 'fastapi>=0.115,<1' 'uvicorn>=0.34,<1' 'httpx>=0.28,<1' 'couchbase>=4.6,<5'
COPY ledger.py server.py comparison.py storage.py memory_trial.py /app/
HEALTHCHECK --interval=10s --timeout=8s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health',timeout=6)"
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090", "--no-access-log"]
