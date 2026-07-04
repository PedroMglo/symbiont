# extrator feature — document ETL and file conversion service
ARG AI_LOCAL_BASE_TAG=dev
FROM ai-local-base:${AI_LOCAL_BASE_TAG} AS extrator-core

LABEL org.opencontainers.image.vendor="ai-local" \
      org.opencontainers.image.title="ai-local extrator"

USER root

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=ailoc:ailoc features/extrator/ /app/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install /app

RUN mkdir -p /data /projects /home/ailoc/.cache \
    && chown -R ailoc:ailoc /data /projects /home/ailoc /app
USER ailoc

EXPOSE 8000
ENV EXTRATOR_CONFIG=/app/config.toml

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -skf https://localhost:8000/health || exit 1

CMD ["uvicorn", "extrator.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "warning"]


FROM extrator-core AS extrator-office
USER root
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        pandoc \
        libreoffice \
    && rm -rf /var/lib/apt/lists/*
USER ailoc


FROM extrator-core AS extrator-ocr
USER root
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/*
USER ailoc


FROM extrator-core AS extrator-docling
USER root
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation "/app[docling]"
RUN if [ -d /usr/local/lib/python3.11/site-packages/rapidocr ]; then \
        chown -R ailoc:ailoc /usr/local/lib/python3.11/site-packages/rapidocr; \
    fi
USER ailoc


FROM extrator-core AS extrator-unstructured
USER root
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation "/app[unstructured]"
USER ailoc


FROM extrator-core AS extrator-all
USER root
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        pandoc \
        texlive-latex-base \
        texlive-latex-recommended \
        texlive-latex-extra \
        texlive-plain-generic \
        lmodern \
        libreoffice \
        tesseract-ocr \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-build-isolation "/app[docling,unstructured]"
RUN if [ -d /usr/local/lib/python3.11/site-packages/rapidocr ]; then \
        chown -R ailoc:ailoc /usr/local/lib/python3.11/site-packages/rapidocr; \
    fi
USER ailoc
