# syntax=docker/dockerfile:1.7
ARG AI_LOCAL_BASE_TAG=dev
FROM ai-local-base:${AI_LOCAL_BASE_TAG}

ENV I18N_CONFIG=/app/config.toml

USER root
COPY features/translation/ /app/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir "/app[neural]" \
    && chown -R ailoc:ailoc /app
COPY context_governor/ /app/context_governor/
RUN chown -R ailoc:ailoc /app/context_governor

USER ailoc

EXPOSE 8590

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, ssl; urllib.request.urlopen('https://localhost:8590/health', context=ssl._create_unverified_context())"

ENTRYPOINT ["entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8590", "--workers", "1", "--log-level", "warning"]
