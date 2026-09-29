FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TRANSFORMERS_NO_ADVISORY_WARNINGS=1

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    python -m pip install --upgrade pip && \
    if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
      python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch>=2.7,<3"; \
    else \
      python -m pip install --no-cache-dir "torch>=2.7,<3"; \
    fi && \
    python -m pip install --no-cache-dir \
      "sentence-transformers>=5.1,<6" \
      "packaging>=24,<26" \
      "agentc>=1.1.0,<1.2" \
      "mcp>=1.27,<2.0" \
      "pydantic>=2.10,<3.0" && \
    python -c "import packaging.version, sentence_transformers, agentc, agentc_cli" && \
    agentc --help >/dev/null

COPY publish.sh /usr/local/bin/publish-streamai-catalog
RUN chmod +x /usr/local/bin/publish-streamai-catalog
ENTRYPOINT ["/usr/local/bin/publish-streamai-catalog"]
