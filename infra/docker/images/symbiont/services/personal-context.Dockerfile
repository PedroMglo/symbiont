# syntax=docker/dockerfile:1.7
# personal_context feature — Calendar/email/RSS context provider
# Build (from repo root): docker build -f features/personal_context/Dockerfile -t ai-local-personal-context:latest .
ARG AI_LOCAL_BASE_TAG=dev
FROM ai-local-base:${AI_LOCAL_BASE_TAG}

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local personal context"

USER root

COPY --chown=ailoc:ailoc features/personal_context/ /app/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation /app/
USER ailoc

EXPOSE 8000
ENV PERSONAL_CONTEXT_PORT=8093
ENV PERSONAL_CONTEXT_DATA_DIR=/data

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -skf https://localhost:8000/health || exit 1

CMD ["uvicorn", "personal_context.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "warning"]
