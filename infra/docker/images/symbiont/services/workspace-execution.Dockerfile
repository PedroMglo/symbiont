# syntax=docker/dockerfile:1.7
# workspace_execution feature - disposable workspace execution manager
ARG AI_LOCAL_BASE_TAG=dev
FROM ai-local-base:${AI_LOCAL_BASE_TAG}

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local workspace execution"

COPY --chown=ailoc:ailoc features/workspace_execution/ /app/
USER root
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        qemu-system-x86 \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation /app/ \
    && pip install --upgrade "setuptools>=83.0.0" "wheel>=0.46.2" "jaraco.context>=6.1.0"
USER ailoc

EXPOSE 8000
ENV WORKSPACE_EXECUTION_SCRATCH_ROOT=/temp/workspace_execution
ENV WORKSPACE_EXECUTION_RUNNER_BACKEND=docker_ephemeral
ENV WORKSPACE_EXECUTION_RUNNER_IMAGE=ai-local-command-sandbox:latest

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -skf https://localhost:8000/health || exit 1

CMD ["uvicorn", "workspace_execution.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "warning"]
