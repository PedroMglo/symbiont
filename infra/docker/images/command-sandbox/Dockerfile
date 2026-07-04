FROM python:3.11-slim-trixie

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local command sandbox"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        coreutils \
        curl \
        docker-cli \
        docker-compose \
        findutils \
        git \
        grep \
        iproute2 \
        redis-server \
        redis-tools \
        ripgrep \
        sed \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pytest>=8.0" \
    && groupadd --gid 10001 cmdsandbox \
    && useradd --uid 10001 --gid cmdsandbox --create-home --shell /usr/sbin/nologin cmdsandbox

USER cmdsandbox:cmdsandbox
WORKDIR /workspace/project

CMD ["pwd"]
