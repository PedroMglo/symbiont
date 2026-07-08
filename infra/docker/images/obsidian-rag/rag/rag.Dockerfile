# syntax=docker/dockerfile:1.7

# --- Build stage ---
FROM python:3.11-slim-trixie AS builder

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local rag"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG AI_LOCAL_GIT_URL_INSTEAD_OF=

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends gcc g++ git openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p -m 0700 /root/.ssh \
    && ssh-keyscan github.com >> /root/.ssh/known_hosts

WORKDIR /app
COPY obsidian-rag/pyproject.toml obsidian-rag/requirements.txt ./
COPY pyproject.toml ai-local/pyproject.toml
COPY config/ ai-local/config/
COPY context_governor/ ai-local/context_governor/
COPY orchestrator/ ai-local/orchestrator/
COPY obsidian-rag/ ./
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=ssh \
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
    pip install --upgrade pip "setuptools>=83.0.0" "wheel>=0.46.2" "jaraco.context>=6.1.0" \
    && pip install /app/ai-local \
    && pip install '.[qdrant,reranker,falkordb,temporal]' "qdrant-client>=1.18,<1.19" \
    && pip install --upgrade "setuptools>=83.0.0" "wheel>=0.46.2" "jaraco.context>=6.1.0"; \
    if [ -n "$git_rewrite_url" ]; then \
        git config --global --remove-section url."$git_rewrite_url" || true; \
    fi

# --- Runtime stage ---
FROM python:3.11-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Patch OS-level vulnerabilities, install curl/openssl for healthchecks and TLS, create non-root user
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends curl openssl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 rag \
    && useradd --uid 1000 --gid rag --create-home rag

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade "setuptools>=83.0.0" "wheel>=0.46.2" "jaraco.context>=6.1.0"
COPY obsidian-rag/ ./

# Entrypoint for secret validation
COPY --chmod=755 infra/docker/images/obsidian-rag/rag/base/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chmod=755 infra/docker/images/obsidian-rag/rag/base/tls-cert.sh /usr/local/bin/ai-local-tls-cert.sh

# Vector store data volume — writable by user rag
RUN mkdir -p /app/data && chown -R rag:rag /app
VOLUME ["/app/data"]

EXPOSE 8484

ENV RAG_API_HOST=0.0.0.0
ENV RAG_API_PORT=8484
ENV SERVICE_NAME=rag

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -skf https://localhost:8484/health || exit 1

USER rag

ENTRYPOINT ["entrypoint.sh"]
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8484"]
