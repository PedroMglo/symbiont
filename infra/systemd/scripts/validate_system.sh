#!/usr/bin/env bash
# validate_system.sh — Pre-flight validation for AI infrastructure
#
# Verifies all critical components are operational.
# Exit 0 = all OK, Exit 1 = failures detected.
#
# Usage:
#   ./scripts/validate_system.sh           # Full validation
#   ./scripts/validate_system.sh --fast    # Skip slow checks (Docker GPU test)

set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

FAST_MODE=false
[[ "${1:-}" == "--fast" ]] && FAST_MODE=true

PASS=0
FAIL=0
WARN=0

check_pass() { echo -e "  ${GREEN}✓${NC} $1"; ((PASS++)); }
check_fail() { echo -e "  ${RED}✗${NC} $1"; ((FAIL++)); }
check_warn() { echo -e "  ${YELLOW}⚠${NC} $1"; ((WARN++)); }

echo -e "${BOLD}═══ System Validation ═══${NC}"
echo

# --- 1. NVIDIA GPU ---
echo -e "${BOLD}GPU & Driver${NC}"
if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
    check_pass "nvidia-smi accessible"
    VRAM_FREE="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1)"
    if [[ "${VRAM_FREE%%.*}" -gt 500 ]]; then
        check_pass "GPU VRAM available: ${VRAM_FREE}MB free"
    else
        check_warn "GPU VRAM low: ${VRAM_FREE}MB free"
    fi
else
    check_fail "nvidia-smi not working"
fi

# --- 2. Docker ---
echo
echo -e "${BOLD}Docker${NC}"
if command -v docker &>/dev/null; then
    if docker info &>/dev/null 2>&1; then
        check_pass "Docker daemon running"

        # NVIDIA runtime
        if docker info 2>/dev/null | grep -q "nvidia"; then
            check_pass "NVIDIA runtime registered"
        else
            check_warn "NVIDIA runtime not in Docker (GPU containers may not work)"
        fi

        # GPU passthrough (skip in fast mode)
        if [[ "$FAST_MODE" == "false" ]]; then
            if docker run --rm --gpus=all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi &>/dev/null 2>&1; then
                check_pass "Docker GPU passthrough works"
            else
                check_warn "Docker GPU test failed (image may need pull)"
            fi
        fi
    else
        check_fail "Docker daemon not accessible"
    fi
else
    check_fail "Docker not installed"
fi

# --- 3. Ollama ---
echo
echo -e "${BOLD}Ollama${NC}"
OLLAMA_URL="https://localhost:11434"

if command -v ollama &>/dev/null; then
    check_pass "Ollama binary found"
else
    check_fail "Ollama not in PATH"
fi

if systemctl is-active --quiet ollama 2>/dev/null; then
    check_pass "Ollama systemd service active"
else
    if pgrep -x ollama &>/dev/null; then
        check_pass "Ollama process running (not systemd)"
    else
        check_fail "Ollama not running"
    fi
fi

if curl -skf "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
    check_pass "Ollama API responding"

    # Check models available
    MODEL_COUNT="$(curl -skf "${OLLAMA_URL}/api/tags" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('models',[])))" 2>/dev/null || echo 0)"
    if [[ "$MODEL_COUNT" -gt 0 ]]; then
        check_pass "Models available: $MODEL_COUNT"
    else
        check_fail "No models installed in Ollama"
    fi

    # Quick generation test (skip in fast mode already handled by warm check)
    LOADED="$(curl -skf "${OLLAMA_URL}/api/ps" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('models',[])))" 2>/dev/null || echo 0)"
    if [[ "$LOADED" -gt 0 ]]; then
        check_pass "Models loaded in VRAM: $LOADED"
    else
        check_warn "No models currently loaded in VRAM (will cold-start on first request)"
    fi
else
    check_fail "Ollama API not responding at $OLLAMA_URL"
fi

# --- 4. Redis ---
echo
echo -e "${BOLD}Redis (Execution Broker)${NC}"
if command -v redis-cli &>/dev/null || docker ps --format '{{.Names}}' 2>/dev/null | grep -q redis; then
    if redis-cli -p 6380 ping &>/dev/null 2>&1; then
        check_pass "Redis responding on port 6380"
    elif docker exec orc-execution-redis redis-cli ping &>/dev/null 2>&1; then
        check_pass "Redis responding (via Docker)"
    else
        check_warn "Redis not responding on port 6380 (execution layer may not work)"
    fi
else
    check_warn "Redis not available (execution layer disabled)"
fi

# --- 5. ClickHouse ---
echo
echo -e "${BOLD}ClickHouse (Observability)${NC}"
if curl -skf "https://localhost:8123/ping" >/dev/null 2>&1; then
    check_pass "ClickHouse responding on port 8123"
else
    check_warn "ClickHouse not responding (observability will use local logs fallback)"
fi

# --- 6. RAG Service ---
echo
echo -e "${BOLD}RAG Service${NC}"
if curl -skf "https://localhost:8484/health" >/dev/null 2>&1; then
    check_pass "RAG service healthy on port 8484"
elif curl -skf "https://localhost:8484/" >/dev/null 2>&1; then
    check_pass "RAG service responding on port 8484"
else
    check_warn "RAG service not responding (context will degrade gracefully)"
fi

# --- 7. Symbiont API ---
echo
echo -e "${BOLD}Symbiont API${NC}"
if curl -skf "https://localhost:8585/health" >/dev/null 2>&1; then
    check_pass "Symbiont API healthy on port 8585"
else
    check_warn "Symbiont API not running (start with: make infra)"
fi

# --- 8. System Resources ---
echo
echo -e "${BOLD}System Resources${NC}"

AVAIL_RAM_MB="$(grep MemAvailable /proc/meminfo | awk '{print int($2/1024)}')"
if [[ "$AVAIL_RAM_MB" -gt 8000 ]]; then
    check_pass "RAM available: ${AVAIL_RAM_MB}MB"
elif [[ "$AVAIL_RAM_MB" -gt 4000 ]]; then
    check_warn "RAM available: ${AVAIL_RAM_MB}MB (moderate pressure)"
else
    check_fail "RAM critically low: ${AVAIL_RAM_MB}MB"
fi

SWAP_USED_MB="$(awk '/SwapTotal/{t=$2} /SwapFree/{f=$2} END{print int((t-f)/1024)}' /proc/meminfo)"
if [[ "$SWAP_USED_MB" -lt 512 ]]; then
    check_pass "Swap usage low: ${SWAP_USED_MB}MB"
elif [[ "$SWAP_USED_MB" -lt 2048 ]]; then
    check_warn "Swap usage: ${SWAP_USED_MB}MB"
else
    check_fail "Swap usage high: ${SWAP_USED_MB}MB (system under memory pressure)"
fi

# --- 9. Worker Image ---
echo
echo -e "${BOLD}Execution Worker Image${NC}"
if command -v docker &>/dev/null && docker image inspect orc-execution-worker:latest &>/dev/null 2>&1; then
    check_pass "Worker image orc-execution-worker:latest exists"
else
    check_warn "Worker image not built (run: docker build -t orc-execution-worker:latest ...)"
fi

# --- Summary ---
echo
echo -e "${BOLD}═══════════════════════════════════════════${NC}"
TOTAL=$((PASS + FAIL + WARN))
echo -e "  Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}, ${YELLOW}${WARN} warnings${NC} / ${TOTAL} total"

if [[ $FAIL -gt 0 ]]; then
    echo -e "  ${RED}VALIDATION FAILED${NC} — fix critical issues before proceeding"
    exit 1
elif [[ $WARN -gt 0 ]]; then
    echo -e "  ${YELLOW}VALIDATION PASSED WITH WARNINGS${NC} — non-critical issues present"
    exit 0
else
    echo -e "  ${GREEN}ALL CHECKS PASSED${NC}"
    exit 0
fi
