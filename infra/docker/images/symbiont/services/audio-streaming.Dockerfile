# Audio Streaming — GPU-enabled realtime audio service.
ARG AI_LOCAL_IMAGE_TAG=dev
FROM ai-local-audio-runtime:${AI_LOCAL_IMAGE_TAG}

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local audio streaming"

USER root
WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
        "httpx>=0.25" \
        "websockets>=12.0" \
    && pip install --upgrade "setuptools>=83.0.0" "wheel>=0.46.2" "jaraco.context>=6.1.0"

COPY agents/audio_transcribe/streaming/ /app/streaming/
RUN chown -R appuser:appuser /app /data

USER appuser

ENV STREAM_HOST=0.0.0.0
ENV STREAM_PORT=8087
ENV AUDIO_OUTPUT_DIR=/data/output
ENV REDIS_URL=redis://redis:6379/0
ENV WHISPER_MODEL=distil-large-v3
ENV WHISPER_DEVICE=auto
ENV WHISPER_COMPUTE_TYPE=int8_float16
ENV GPU_WORKERS=1
ENV LOG_LEVEL=INFO
ENV OLLAMA_BASE_URL=https://host.docker.internal:11434

EXPOSE 8087

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request, ssl; urllib.request.urlopen('https://localhost:8087/health', context=ssl._create_unverified_context())" || exit 1

CMD ["python", "-m", "streaming.main"]
