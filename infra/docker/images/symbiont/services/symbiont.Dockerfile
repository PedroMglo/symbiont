# syntax=docker/dockerfile:1.7
# =============================================================================
# ai-symbiont — Multi-stage Dockerfile
# =============================================================================
# Build (from repo root):
#   docker build -f infra/docker/images/symbiont/services/symbiont.Dockerfile -t ai-local-symbiont:latest .
# =============================================================================
ARG AI_LOCAL_BASE_TAG=dev
FROM ai-local-base:${AI_LOCAL_BASE_TAG} AS runtime

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local symbiont"

USER root

# Install Docker CLI + Compose plugin (for lifecycle container management)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Install symbiont-specific deps (not in base)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
        "langgraph>=0.2" \
        "langchain-core>=0.3" \
        "rich>=13.0" \
        "icalendar>=6.0" \
        "feedparser>=6.0" \
        "redis[hiredis]>=5.0" \
        "docker>=7.0"

# Copy runtime source owned by the mono-repo.
COPY --chown=ailoc:ailoc orchestrator/ /app/orchestrator/
COPY --chown=ailoc:ailoc context_governor/ /app/context_governor/
COPY --chown=ailoc:ailoc agents/ /app/agents/
COPY --chown=ailoc:ailoc features/ /app/features/
COPY --chown=ailoc:ailoc config/ /app/config/

# Copy external owner manifests consumed as runtime metadata. This keeps
# dispatch behavior behind APIs while allowing the orchestrator to expose the
# complete owner capability baseline from inside the container image.
RUN mkdir -p /app/storage_guardian
COPY --chown=ailoc:ailoc storage_guardian/service_capabilities.toml /app/storage_guardian/service_capabilities.toml
COPY --chown=ailoc:ailoc infra/security/ /app/infra/security/

# Config and data directories
RUN mkdir -p /app/data /app/config && chown -R ailoc:ailoc /app

USER ailoc

EXPOSE 8585

ENV SERVICE_NAME=symbiont
ENV ORC_API_HOST=0.0.0.0
ENV ORC_API_PORT=8585
ENV OLLAMA_BASE_URL=https://host.docker.internal:11434
ENV PYTHONPATH=/app

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -skf https://localhost:8585/health || exit 1

CMD ["uvicorn", "orchestrator.gateway.app:app", "--host", "0.0.0.0", "--port", "8585", "--workers", "1", "--log-level", "warning"]
