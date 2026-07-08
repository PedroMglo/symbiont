# syntax=docker/dockerfile:1.7
# execution_policy_operator agent — deterministic command risk evidence
ARG AI_LOCAL_BASE_TAG=dev
FROM ai-local-base:${AI_LOCAL_BASE_TAG}

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local execution policy operator"

USER root
COPY --chown=ailoc:ailoc agents/execution_policy_operator/ /app/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation /app/ \
    && pip install --upgrade "setuptools>=83.0.0" "wheel>=0.46.2" "jaraco.context>=6.1.0"
USER ailoc

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -skf https://localhost:8000/health || exit 1

CMD ["uvicorn", "execution_policy_operator.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "warning"]
