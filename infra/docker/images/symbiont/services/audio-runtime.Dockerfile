# Shared CUDA/audio runtime for ai-local audio services.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local audio runtime"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        python3.11 \
        python3.11-venv \
        python3-pip \
        git \
        ffmpeg \
        libsndfile1 \
        libchromaprint-tools \
        curl \
        openssl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

WORKDIR /app

COPY agents/audio_transcribe/requirements.txt agents/audio_transcribe/requirements-gpu.txt /tmp/audio-runtime/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip "setuptools>=83.0.0" "wheel>=0.46.2" "jaraco.context>=6.1.0" \
    && pip install -r /tmp/audio-runtime/requirements.txt -r /tmp/audio-runtime/requirements-gpu.txt \
    && pip install --upgrade "setuptools>=83.0.0" "wheel>=0.46.2" "jaraco.context>=6.1.0" \
    && rm -rf /tmp/audio-runtime

COPY infra/docker/images/symbiont/base/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY infra/docker/images/symbiont/base/tls-cert.sh /usr/local/bin/ai-local-tls-cert.sh
RUN useradd -m -s /bin/bash appuser \
    && mkdir -p /data/input /data/output /data/models /data/tmp /home/appuser/.cache/huggingface/hub \
    && chown -R appuser:appuser /app /data /home/appuser/.cache \
    && chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/ai-local-tls-cert.sh

USER appuser
ENTRYPOINT ["entrypoint.sh"]
