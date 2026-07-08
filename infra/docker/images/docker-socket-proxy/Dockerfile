# syntax=docker/dockerfile:1.7

ARG DOCKER_SOCKET_PROXY_UPSTREAM_TAG=v0.4.2
FROM tecnativa/docker-socket-proxy:${DOCKER_SOCKET_PROXY_UPSTREAM_TAG}

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local docker socket proxy"

USER root

RUN apk update \
    && apk upgrade --no-cache \
    && rm -rf /var/cache/apk/*

USER haproxy
