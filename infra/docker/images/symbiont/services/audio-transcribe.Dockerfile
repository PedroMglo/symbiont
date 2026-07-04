# Audio Transcribe — batch transcription service.
ARG AI_LOCAL_IMAGE_TAG=dev
FROM ai-local-audio-runtime:${AI_LOCAL_IMAGE_TAG}

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local audio transcribe"

USER root
WORKDIR /app

COPY agents/audio_transcribe/audio_transcribe/ ./audio_transcribe/
COPY agents/audio_transcribe/pyproject.toml .
COPY agents/audio_transcribe/config.toml ./config.toml
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install .

RUN chown -R appuser:appuser /app /data
USER appuser

ENV AUDIO_TRANSCRIBE_CONFIG=/app/config.toml

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, ssl; urllib.request.urlopen('https://localhost:8080/health', context=ssl._create_unverified_context())" || exit 1

CMD ["uvicorn", "audio_transcribe.api:app", "--host", "0.0.0.0", "--port", "8080"]
