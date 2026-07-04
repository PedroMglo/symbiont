#!/usr/bin/env bash
# =============================================================================
# validate-docker.sh — Validate Docker environment for ai-local
# =============================================================================
# Checks: Docker daemon, base image, secrets, compose config, network, ports
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$ROOT_DIR"

errors=0
warnings=0
AI_LOCAL_DOCKER_CONTEXT="${AI_LOCAL_DOCKER_CONTEXT:-${DOCKER_CONTEXT:-default}}"
AI_COMPOSE_PROFILES_SELECTED="${AI_COMPOSE_PROFILES:-core,storage}"
DOCKER=(docker --context "$AI_LOCAL_DOCKER_CONTEXT")

check() {
    local label="$1"
    local result="$2"
    if [ "$result" = "ok" ]; then
        printf "  ✓ %-40s %s\n" "$label" ""
    elif [ "$result" = "warn" ]; then
        printf "  ⚠ %-40s %s\n" "$label" "(warning)"
        warnings=$((warnings + 1))
    else
        printf "  ✗ %-40s %s\n" "$label" "$result"
        errors=$((errors + 1))
    fi
}

echo "══════════════════════════════════════════════"
echo "  Docker Environment Validation"
echo "══════════════════════════════════════════════"
echo ""

# --- Docker daemon ---
echo "Docker Runtime:"
if command -v docker &>/dev/null; then
    check "Docker CLI installed" "ok"
else
    check "Docker CLI installed" "NOT FOUND"
fi

check "Docker context: $AI_LOCAL_DOCKER_CONTEXT" "ok"

if "${DOCKER[@]}" info &>/dev/null 2>&1; then
    check "Docker daemon running" "ok"
else
    check "Docker daemon running" "NOT RUNNING"
fi

echo ""

# --- Base image ---
echo "Base Image:"
AI_LOCAL_BASE_TAG="${AI_LOCAL_BASE_TAG:-dev}"
if "${DOCKER[@]}" image inspect "ai-local-base:${AI_LOCAL_BASE_TAG}" &>/dev/null 2>&1; then
    check "ai-local-base:${AI_LOCAL_BASE_TAG} exists" "ok"
else
    check "ai-local-base:${AI_LOCAL_BASE_TAG} exists" "NOT BUILT (run: make build-base)"
fi

echo ""

# --- Secrets ---
echo "Secrets:"
secret_output="$(AI_COMPOSE_PROFILES="$AI_COMPOSE_PROFILES_SELECTED" python scripts/docker_policy.py required-secrets --lines 2>&1)" || secret_status=$?
secret_status="${secret_status:-0}"
if [ "$secret_status" -ne 0 ]; then
    check "secret catalog" "INVALID"
    echo "$secret_output" | sed 's/^/      /'
elif [ -n "$secret_output" ]; then
    while IFS=$'\t' read -r name path profiles; do
        if [ -z "${name:-}" ]; then
            continue
        fi
        if [ -s "$path" ]; then
            mode="$(stat -c '%a' "$path" 2>/dev/null || stat -f '%Lp' "$path" 2>/dev/null || echo unknown)"
            if [ "$mode" = "600" ]; then
                check "$name" "ok"
            else
                check "$name" "mode $mode (expected 600)"
            fi
        else
            check "$name" "MISSING/EMPTY"
        fi
    done <<< "$secret_output"
else
    check "Secrets for profiles $AI_COMPOSE_PROFILES_SELECTED" "ok"
fi
unset secret_status
check "Secret profile source" "ok"
echo "      profiles=$AI_COMPOSE_PROFILES_SELECTED"

echo ""

# --- Compose config ---
echo "Compose Configuration:"
profile_contract_output="$(python infra/docker/scripts/validate_compose_profiles.py 2>&1)" || profile_contract_status=$?
profile_contract_status="${profile_contract_status:-0}"
if [ "$profile_contract_status" -eq 0 ]; then
    check "compose profile contract" "ok"
else
    check "compose profile contract" "INVALID"
    echo "$profile_contract_output" | sed 's/^/      /'
fi
unset profile_contract_status

observability_output="$(python infra/docker/scripts/validate_observability_stack.py 2>&1)" || observability_status=$?
observability_status="${observability_status:-0}"
if [ "$observability_status" -eq 0 ]; then
    check "observability stack contract" "ok"
else
    check "observability stack contract" "INVALID"
    echo "$observability_output" | sed 's/^/      /'
fi
unset observability_status

compose_project_output="$(python scripts/docker_policy.py compose-projects --lines 2>&1)" || compose_project_status=$?
compose_project_status="${compose_project_status:-0}"
if [ -n "$compose_project_output" ]; then
    parsed_project_line=0
    while IFS=$'\t' read -r status name role workdir; do
        if [ -z "${status:-}" ]; then
            continue
        fi
        if [ "$status" != "pass" ] && [ "$status" != "fail" ]; then
            continue
        fi
        parsed_project_line=1
        if [ "$status" = "pass" ]; then
            check "$name compose valid" "ok"
        else
            check "$name compose valid" "INVALID"
            echo "      role=$role workdir=$workdir"
        fi
    done <<< "$compose_project_output"
    if [ "$parsed_project_line" -eq 0 ]; then
        check "compose projects catalog" "INVALID"
        echo "$compose_project_output" | sed 's/^/      /'
    fi
else
    check "compose projects catalog" "INVALID"
fi
unset compose_project_status

echo ""

# --- Network ---
echo "Network:"
if "${DOCKER[@]}" network inspect ai-local-net &>/dev/null 2>&1; then
    check "ai-local-net exists" "ok"
else
    check "ai-local-net exists" "warn"
fi

echo ""

# --- Port availability (only public/gateway ports) ---
echo "Port Availability (public services only):"
echo "  NOTE: Internal services use Docker DNS — no host ports needed."
if command -v lsof &>/dev/null; then
    used=0
    port_output="$(AI_COMPOSE_PROFILES="$AI_COMPOSE_PROFILES_SELECTED" python scripts/docker_policy.py host-ports --lines 2>&1)" || port_status=$?
    port_status="${port_status:-0}"
    if [ "$port_status" -ne 0 ]; then
        check "host port catalog" "INVALID"
        echo "$port_output" | sed 's/^/      /'
    fi
    while IFS=$'\t' read -r service port bind profiles; do
        if [ -z "${port:-}" ]; then
            continue
        fi
        pid=$(lsof -ti ":$port" 2>/dev/null | head -1 || true)
        if [ -n "$pid" ]; then
            used=1
            name=$(ps -p "$pid" -o comm= 2>/dev/null || echo "unknown")
            check "Port $port ($service)" "warn"
            echo "      in use by $name (PID $pid), expected bind ${bind:-127.0.0.1}"
        fi
    done <<< "$port_output"
    unset port_status
    if [ "$used" -eq 0 ]; then
        check "All public ports free" "ok"
    fi
else
    check "lsof available for port checks" "warn"
fi

echo ""
echo "══════════════════════════════════════════════"
if [ $errors -gt 0 ]; then
    echo "  RESULT: $errors error(s), $warnings warning(s)"
    echo "  Fix errors before starting services."
    exit 1
else
    echo "  RESULT: All checks passed ($warnings warning(s))"
    echo "  Ready to: make infra"
fi
