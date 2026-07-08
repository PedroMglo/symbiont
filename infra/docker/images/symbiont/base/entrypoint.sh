#!/bin/sh
# =============================================================================
# ai-local — Container entrypoint wrapper
# =============================================================================
# Reads Docker secrets from /run/secrets/ via *_FILE env vars and exports
# them as the corresponding env var (without _FILE suffix).
# Then validates that required API keys are present before starting the service.
#
# Pattern:
#   ORC_SYMBIONT_API_KEY_FILE=/run/secrets/orc_api_key
#   → reads file content → exports ORC_SYMBIONT_API_KEY=<content>
#
# Usage in Dockerfile:
#   COPY infra/docker/images/symbiont/base/entrypoint.sh /usr/local/bin/entrypoint.sh
#   ENTRYPOINT ["entrypoint.sh"]
#   CMD ["uvicorn", "..."]
# =============================================================================
set -e

# ---------------------------------------------------------------------------
# Load secrets: for every env var ending in _FILE, read the file and export
# the value under the var name without the _FILE suffix.
# ---------------------------------------------------------------------------
load_secrets() {
    env_file="${TMPDIR:-/tmp}/ai-local-env.$$"
    env > "$env_file"
    while IFS='=' read -r var_file _; do
        case "$var_file" in
            *_FILE) ;;
            *) continue ;;
        esac
        case "$var_file" in
            ""|[0-9]*|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_]*)
                continue
                ;;
        esac
        case "$var_file" in
            SSL_CERT_FILE|AI_LOCAL_TLS_CERT_FILE|AI_LOCAL_TLS_KEY_FILE|AI_LOCAL_TLS_CA_CERT_FILE|\
            AI_LOCAL_TLS_CA_KEY_FILE|AI_LOCAL_TLS_CA_BUNDLE_FILE|AI_TELEMETRY_AUTHORITY_CA_BUNDLE_FILE)
                continue
                ;;
        esac
        eval "value=\${$var_file:-}"
        var_name="${var_file%_FILE}"
        if [ -f "$value" ]; then
            secret_value="$(cat "$value")"
            export "$var_name"="$secret_value"
            echo "INFO: Loaded secret for ${var_name} from ${value}"
        else
            echo "WARN: Secret file not found: ${value} (for ${var_name})" >&2
        fi
    done < "$env_file"
    rm -f "$env_file"
}

# ---------------------------------------------------------------------------
# Validate required environment variables per service
# ---------------------------------------------------------------------------
validate_env() {
    missing=0

    case "${SERVICE_NAME:-unknown}" in
        symbiont)
            if [ -z "${ORC_SYMBIONT_API_KEY:-}" ]; then
                echo "FATAL: ORC_SYMBIONT_API_KEY is empty. Service cannot start without authentication." >&2
                missing=1
            fi
            ;;
        rag)
            if [ -z "${RAG_API_API_KEY:-}" ]; then
                echo "FATAL: RAG_API_API_KEY is empty. Service cannot start without authentication." >&2
                missing=1
            fi
            ;;
        audio-transcribe|audio-streaming)
            if [ -z "${AUDIO_TRANSCRIBE_API_KEY:-}" ]; then
                echo "FATAL: AUDIO_TRANSCRIBE_API_KEY is empty. Service cannot start without authentication." >&2
                missing=1
            fi
            ;;
        reasoning-and-response|\
        research|local-evidence-operator|execution-policy-operator|material-builder|\
        personal-context|extrator|translation|workspace-execution|material-execution-kernel)
            if [ -z "${API_KEY:-}" ]; then
                echo "FATAL: API_KEY is empty for service '${SERVICE_NAME}'. Service cannot start without authentication." >&2
                missing=1
            fi
            ;;
        storage-guardian)
            if [ -z "${STORAGE_GUARDIAN_INTERNAL_TOKEN:-}" ]; then
                echo "FATAL: STORAGE_GUARDIAN_INTERNAL_TOKEN is empty. Service cannot start without authentication." >&2
                missing=1
            fi
            ;;
        *)
            echo "WARN: Unknown SERVICE_NAME='${SERVICE_NAME:-}'. No auth validation applied." >&2
            ;;
    esac

    if [ -n "${OLLAMA_BASE_URL:-}" ]; then
        echo "INFO: Ollama URL configured: ${OLLAMA_BASE_URL}"
    fi

    if [ "$missing" -ne 0 ]; then
        echo "" >&2
        echo "Create required local secrets in infra/docker/secrets/ before starting services." >&2
        exit 1
    fi
}

uvicorn_needs_tls_args() {
    if [ "${1:-}" != "uvicorn" ]; then
        return 1
    fi

    for arg in "$@"; do
        if [ "$arg" = "--ssl-certfile" ] || [ "$arg" = "--ssl-keyfile" ]; then
            return 1
        fi
    done

    return 0
}

# --- Main ---
load_secrets
validate_env
. /usr/local/bin/ai-local-tls-cert.sh

if uvicorn_needs_tls_args "$@"; then
    exec "$@" \
        --ssl-certfile "${AI_LOCAL_TLS_CERT_FILE}" \
        --ssl-keyfile "${AI_LOCAL_TLS_KEY_FILE}"
fi
exec "$@"
