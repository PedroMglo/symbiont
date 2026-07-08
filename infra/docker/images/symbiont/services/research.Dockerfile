# syntax=docker/dockerfile:1.7
# research feature — RAG/CAG semantic search context provider
# Build (from repo root): docker build -f features/research/Dockerfile -t ai-local-research:latest .
ARG AI_LOCAL_BASE_TAG=dev
FROM ai-local-base:${AI_LOCAL_BASE_TAG}

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local research"

USER root
COPY --chown=ailoc:ailoc features/research/ /app/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation /app/ \
    && pip install --upgrade "setuptools>=83.0.0" "wheel>=0.46.2" "jaraco.context>=6.1.0"
USER ailoc

EXPOSE 8000
ENV RESEARCH_PORT=8090
ENV RESEARCH_RAG_URL=https://rag:8484
ENV OLLAMA_BASE_URL=https://host.docker.internal:11434

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -skf https://localhost:8000/health || exit 1

CMD ["uvicorn", "research.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "warning"]
