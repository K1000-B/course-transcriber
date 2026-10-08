
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install CPU-only PyTorch for AMD64 and ARM64
RUN python -m pip install --no-cache-dir \
    "torch==2.6.0+cpu" \
    --index-url https://download.pytorch.org/whl/cpu

# Install application dependencies
COPY requirements.txt /tmp/requirements.txt

RUN python -m pip install --no-cache-dir \
    -r /tmp/requirements.txt

# Application
WORKDIR /workspace
COPY . .

CMD ["python", "-u", "main.py"]
