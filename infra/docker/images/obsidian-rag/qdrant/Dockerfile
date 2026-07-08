# syntax=docker/dockerfile:1.7

ARG QDRANT_UPSTREAM_TAG=v1.18.2
FROM qdrant/qdrant:${QDRANT_UPSTREAM_TAG}

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local qdrant"

USER root

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

USER 1000:1000
