# syntax=docker/dockerfile:1.7
# reasoning_and_response agent - read-only cognitive provider family
ARG AI_LOCAL_BASE_TAG=dev
FROM ai-local-base:${AI_LOCAL_BASE_TAG}

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local reasoning and response"

USER root
COPY --chown=ailoc:ailoc agents/reasoning_and_response/ /app/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation /app/
COPY --chown=ailoc:ailoc context_governor/ /app/context_governor/
USER ailoc

EXPOSE 8000
ENV REASONING_AND_RESPONSE_PORT=8000
ENV OLLAMA_BASE_URL=https://host.docker.internal:11434

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -skf https://localhost:8000/health || exit 1

CMD ["uvicorn", "reasoning_and_response.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "warning"]
