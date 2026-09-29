FROM streamai-capella-memory-base:1.0.0-rc2
USER 0
COPY apply-nvidia-compat.py /tmp/apply-nvidia-compat.py
COPY trial_context.py /tmp/trial_context.py
RUN ["/opt/venv/bin/python", "/tmp/apply-nvidia-compat.py"]
USER 65532
