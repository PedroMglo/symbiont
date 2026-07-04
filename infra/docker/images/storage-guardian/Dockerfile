FROM python:3.12-slim

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local storage guardian"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG AI_LOCAL_GIT_URL_INSTEAD_OF=

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client openssl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p -m 0700 /root/.ssh \
    && ssh-keyscan github.com >> /root/.ssh/known_hosts

WORKDIR /app
COPY pyproject.toml /app/ai-local/pyproject.toml
COPY config /app/ai-local/config
COPY orchestrator /app/ai-local/orchestrator
COPY storage_guardian/pyproject.toml /app/storage_guardian/pyproject.toml
COPY storage_guardian/src /app/storage_guardian/src
COPY infra/docker/images/symbiont/base/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY infra/docker/images/symbiont/base/tls-cert.sh /usr/local/bin/ai-local-tls-cert.sh
RUN --mount=type=ssh \
    --mount=type=secret,id=github_token,required=false \
    set -eu; \
    git_rewrite_url=""; \
    if [ -s /run/secrets/github_token ]; then \
        github_token="$(cat /run/secrets/github_token)"; \
        git_rewrite_url="https://x-access-token:${github_token}@github.com/"; \
    elif [ -n "$AI_LOCAL_GIT_URL_INSTEAD_OF" ]; then \
        git_rewrite_url="$AI_LOCAL_GIT_URL_INSTEAD_OF"; \
    fi; \
    if [ -n "$git_rewrite_url" ]; then \
        git config --global url."$git_rewrite_url".insteadOf "ssh://git@github.com/"; \
    fi; \
    pip install --no-cache-dir /app/ai-local /app/storage_guardian; \
    if [ -n "$git_rewrite_url" ]; then \
        git config --global --remove-section url."$git_rewrite_url" || true; \
    fi
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/ai-local-tls-cert.sh

USER 1000:1000
EXPOSE 8730
ENTRYPOINT ["entrypoint.sh"]
CMD ["uvicorn", "storage_guardian.app:app", "--host", "0.0.0.0", "--port", "8730"]
