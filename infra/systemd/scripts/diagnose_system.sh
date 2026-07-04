#!/usr/bin/env bash
# diagnose_system.sh — Full AI infrastructure diagnostic (non-destructive, read-only)
#
# Covers: GPU/NVIDIA, Ollama, Docker, RAM, swap, I/O, CPU, NVMe, network, kernel
#
# Usage:
#   ./scripts/diagnose_system.sh              # Full diagnostic to stdout
#   ./scripts/diagnose_system.sh --json       # JSON output to benchmarks/
#   ./scripts/diagnose_system.sh --quiet      # Summary only (exit code 0=healthy, 1=issues)

set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
JSON_MODE=false
QUIET_MODE=false
ISSUES=()

for arg in "$@"; do
    case "$arg" in
        --json) JSON_MODE=true ;;
        --quiet) QUIET_MODE=true ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
section() {
    if [[ "$QUIET_MODE" == "false" ]]; then
        echo
        echo -e "${BOLD}═══ $1 ═══${NC}"
        echo
    fi
}

ok() {
    [[ "$QUIET_MODE" == "false" ]] && echo -e "  ${GREEN}✓${NC} $1"
}

warn() {
    [[ "$QUIET_MODE" == "false" ]] && echo -e "  ${YELLOW}⚠${NC} $1"
    ISSUES+=("WARN: $1")
}

fail() {
    [[ "$QUIET_MODE" == "false" ]] && echo -e "  ${RED}✗${NC} $1"
    ISSUES+=("FAIL: $1")
}

info() {
    [[ "$QUIET_MODE" == "false" ]] && echo -e "  ${CYAN}ℹ${NC} $1"
}

# JSON accumulator
declare -A JSON_DATA

json_set() {
    JSON_DATA["$1"]="$2"
}

# ---------------------------------------------------------------------------
# 1. System Overview
# ---------------------------------------------------------------------------
section "System Overview"

HOSTNAME="$(hostname)"
KERNEL="$(uname -r)"
OS="$(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2 | tr -d '"' || uname -s)"
UPTIME="$(uptime -p 2>/dev/null || uptime)"
CPU_MODEL="$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | xargs || echo 'unknown')"
CPU_CORES="$(nproc 2>/dev/null || echo '?')"
CPU_GOVERNOR="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo 'unknown')"

info "Hostname: $HOSTNAME"
info "OS: $OS"
info "Kernel: $KERNEL"
info "CPU: $CPU_MODEL ($CPU_CORES cores)"
info "CPU Governor: $CPU_GOVERNOR"
info "Uptime: $UPTIME"

json_set "hostname" "$HOSTNAME"
json_set "kernel" "$KERNEL"
json_set "os" "$OS"
json_set "cpu_model" "$CPU_MODEL"
json_set "cpu_cores" "$CPU_CORES"

# ---------------------------------------------------------------------------
# 2. Memory & Swap
# ---------------------------------------------------------------------------
section "Memory & Swap"

TOTAL_RAM_KB="$(grep MemTotal /proc/meminfo | awk '{print $2}')"
AVAIL_RAM_KB="$(grep MemAvailable /proc/meminfo | awk '{print $2}')"
TOTAL_RAM_MB=$((TOTAL_RAM_KB / 1024))
AVAIL_RAM_MB=$((AVAIL_RAM_KB / 1024))
USED_RAM_MB=$((TOTAL_RAM_MB - AVAIL_RAM_MB))
RAM_PERCENT=$((USED_RAM_MB * 100 / TOTAL_RAM_MB))

SWAP_TOTAL_KB="$(grep SwapTotal /proc/meminfo | awk '{print $2}')"
SWAP_USED_KB="$((SWAP_TOTAL_KB - $(grep SwapFree /proc/meminfo | awk '{print $2}')))"
SWAP_TOTAL_MB=$((SWAP_TOTAL_KB / 1024))
SWAP_USED_MB=$((SWAP_USED_KB / 1024))

SWAPPINESS="$(cat /proc/sys/vm/swappiness 2>/dev/null || echo '?')"
DIRTY_RATIO="$(cat /proc/sys/vm/dirty_ratio 2>/dev/null || echo '?')"
DIRTY_BG_RATIO="$(cat /proc/sys/vm/dirty_background_ratio 2>/dev/null || echo '?')"

info "RAM: ${USED_RAM_MB}MB / ${TOTAL_RAM_MB}MB (${RAM_PERCENT}% used, ${AVAIL_RAM_MB}MB available)"
info "Swap: ${SWAP_USED_MB}MB / ${SWAP_TOTAL_MB}MB"
info "vm.swappiness: $SWAPPINESS"
info "vm.dirty_ratio: $DIRTY_RATIO"
info "vm.dirty_background_ratio: $DIRTY_BG_RATIO"

if [[ $SWAP_USED_MB -gt 2048 ]]; then
    warn "High swap usage: ${SWAP_USED_MB}MB (>2GB) — system may be under memory pressure"
elif [[ $SWAP_USED_MB -gt 512 ]]; then
    warn "Moderate swap usage: ${SWAP_USED_MB}MB"
else
    ok "Swap usage normal: ${SWAP_USED_MB}MB"
fi

if [[ "$SWAPPINESS" -gt 30 ]]; then
    warn "vm.swappiness=$SWAPPINESS (high for AI workloads, recommend 10)"
else
    ok "vm.swappiness=$SWAPPINESS"
fi

json_set "ram_total_mb" "$TOTAL_RAM_MB"
json_set "ram_available_mb" "$AVAIL_RAM_MB"
json_set "swap_used_mb" "$SWAP_USED_MB"
json_set "swap_total_mb" "$SWAP_TOTAL_MB"
json_set "vm_swappiness" "$SWAPPINESS"

# ---------------------------------------------------------------------------
# 3. NVIDIA GPU
# ---------------------------------------------------------------------------
section "NVIDIA GPU"

if command -v nvidia-smi &>/dev/null; then
    ok "nvidia-smi found"

    DRIVER_VERSION="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits 2>/dev/null | head -1 || echo 'error')"
    CUDA_VERSION="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 && nvidia-smi | grep -oP 'CUDA Version: \K[0-9.]+' || echo 'unknown')"
    GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo 'unknown')"
    GPU_VRAM_TOTAL="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo '0')"
    GPU_VRAM_USED="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 || echo '0')"
    GPU_VRAM_FREE="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1 || echo '0')"
    GPU_UTIL="$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 || echo '0')"
    GPU_TEMP="$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 || echo '0')"
    GPU_POWER="$(nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits 2>/dev/null | head -1 || echo '0')"
    GPU_POWER_LIMIT="$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits 2>/dev/null | head -1 || echo '0')"
    CUDA_VER="$(nvidia-smi | grep -oP 'CUDA Version: \K[0-9.]+' 2>/dev/null || echo 'unknown')"

    info "GPU: $GPU_NAME"
    info "Driver: $DRIVER_VERSION"
    info "CUDA: $CUDA_VER"
    info "VRAM: ${GPU_VRAM_USED}MB / ${GPU_VRAM_TOTAL}MB (${GPU_VRAM_FREE}MB free)"
    info "GPU Utilization: ${GPU_UTIL}%"
    info "Temperature: ${GPU_TEMP}°C"
    info "Power: ${GPU_POWER}W / ${GPU_POWER_LIMIT}W"

    if [[ "${GPU_VRAM_USED%%.*}" -gt 7200 ]]; then
        warn "VRAM usage very high: ${GPU_VRAM_USED}MB / ${GPU_VRAM_TOTAL}MB"
    elif [[ "${GPU_VRAM_USED%%.*}" -gt 6500 ]]; then
        warn "VRAM usage high: ${GPU_VRAM_USED}MB / ${GPU_VRAM_TOTAL}MB"
    else
        ok "VRAM usage: ${GPU_VRAM_USED}MB / ${GPU_VRAM_TOTAL}MB"
    fi

    if [[ "${GPU_TEMP%%.*}" -gt 85 ]]; then
        warn "GPU temperature high: ${GPU_TEMP}°C"
    else
        ok "GPU temperature: ${GPU_TEMP}°C"
    fi

    json_set "gpu_name" "$GPU_NAME"
    json_set "gpu_driver" "$DRIVER_VERSION"
    json_set "gpu_cuda" "$CUDA_VER"
    json_set "gpu_vram_total_mb" "$GPU_VRAM_TOTAL"
    json_set "gpu_vram_used_mb" "$GPU_VRAM_USED"
    json_set "gpu_vram_free_mb" "$GPU_VRAM_FREE"
    json_set "gpu_util_percent" "$GPU_UTIL"
    json_set "gpu_temp_celsius" "$GPU_TEMP"
    json_set "gpu_power_watts" "$GPU_POWER"

    # GPU processes
    echo
    info "GPU Processes:"
    nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv,noheader 2>/dev/null | while IFS= read -r line; do
        [[ -n "$line" ]] && info "  $line"
    done || info "  (none)"
else
    fail "nvidia-smi not found — GPU monitoring unavailable"
fi

# ---------------------------------------------------------------------------
# 4. NVIDIA Container Toolkit
# ---------------------------------------------------------------------------
section "NVIDIA Container Toolkit (Docker GPU)"

if command -v docker &>/dev/null; then
    ok "Docker found: $(docker --version 2>/dev/null | head -1)"

    # Check nvidia runtime
    if docker info 2>/dev/null | grep -q "nvidia"; then
        ok "NVIDIA runtime registered in Docker"
    else
        warn "NVIDIA runtime NOT found in Docker info"
    fi

    # Check nvidia-container-toolkit
    if command -v nvidia-container-toolkit &>/dev/null || dpkg -l 2>/dev/null | grep -q nvidia-container-toolkit; then
        ok "nvidia-container-toolkit installed"
    elif command -v nvidia-ctk &>/dev/null; then
        ok "nvidia-ctk found"
    else
        warn "nvidia-container-toolkit not detected (may still work via runtime config)"
    fi

    # Test GPU passthrough (non-destructive, quick)
    info "Testing Docker GPU access (nvidia-smi in container)..."
    if docker run --rm --gpus=all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi &>/dev/null 2>&1; then
        ok "Docker GPU passthrough works (--gpus=all)"
    else
        # Try with runtime flag
        if docker run --rm --runtime=nvidia nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi &>/dev/null 2>&1; then
            ok "Docker GPU passthrough works (--runtime=nvidia)"
        else
            warn "Docker GPU passthrough test failed (image may not be pulled yet)"
            info "  Try: docker pull nvidia/cuda:12.6.0-base-ubuntu24.04"
        fi
    fi
else
    fail "Docker not found"
fi

# ---------------------------------------------------------------------------
# 5. Ollama Service
# ---------------------------------------------------------------------------
section "Ollama Service"

if command -v ollama &>/dev/null; then
    ok "Ollama installed: $(ollama --version 2>/dev/null || echo 'version unknown')"
else
    fail "Ollama not found in PATH"
fi

# Systemd status
if systemctl is-active --quiet ollama 2>/dev/null; then
    ok "Ollama systemd service: active"

    # Show environment
    OLLAMA_ENV="$(systemctl show ollama --property=Environment 2>/dev/null | sed 's/Environment=//')"
    if [[ -n "$OLLAMA_ENV" && "$OLLAMA_ENV" != "" ]]; then
        info "Systemd environment:"
        echo "$OLLAMA_ENV" | tr ' ' '\n' | while read -r var; do
            [[ -n "$var" ]] && info "  $var"
        done
        if command -v nvidia-smi &>/dev/null; then
            if echo "$OLLAMA_ENV" | grep -Eq '(^| )CUDA_VISIBLE_DEVICES=($| )'; then
                fail "Ollama is forced CPU-only: CUDA_VISIBLE_DEVICES is empty"
            fi
            if echo "$OLLAMA_ENV" | grep -Eq '(^| )OLLAMA_NUM_GPU=0($| )'; then
                fail "Ollama is forced CPU-only: OLLAMA_NUM_GPU=0"
            fi
        fi
    fi

    # Check override file
    OVERRIDE_DIR="/etc/systemd/system/ollama.service.d"
    if [[ -d "$OVERRIDE_DIR" ]]; then
        info "Override directory exists: $OVERRIDE_DIR"
        for f in "$OVERRIDE_DIR"/*.conf; do
            [[ -f "$f" ]] && info "  $(basename "$f"): $(grep -c Environment "$f" 2>/dev/null || echo 0) env vars"
        done
    else
        warn "No systemd override directory — performance tuning may not be applied"
    fi
else
    warn "Ollama not running as systemd service"
    # Check if running as process
    if pgrep -x ollama &>/dev/null; then
        info "Ollama running as process (PID: $(pgrep -x ollama | head -1))"
    else
        fail "Ollama process not running"
    fi
fi

# Ollama process GPU check
OLLAMA_PID="$(pgrep -x ollama 2>/dev/null | head -1 || echo '')"
if [[ -n "$OLLAMA_PID" ]] && command -v nvidia-smi &>/dev/null; then
    GPU_PROCS="$(nvidia-smi --query-compute-apps=pid,used_gpu_memory --format=csv,noheader 2>/dev/null || echo '')"
    if echo "$GPU_PROCS" | grep -q "$OLLAMA_PID"; then
        OLLAMA_VRAM="$(echo "$GPU_PROCS" | grep "$OLLAMA_PID" | awk -F', ' '{print $2}')"
        ok "Ollama using GPU (PID $OLLAMA_PID, VRAM: $OLLAMA_VRAM)"
    else
        info "Ollama process not currently using GPU (no model loaded or CPU-only mode)"
    fi
fi

# ---------------------------------------------------------------------------
# 6. Ollama API Health & Models
# ---------------------------------------------------------------------------
section "Ollama API & Models"

OLLAMA_URL="https://localhost:11434"

# API healthcheck
if curl -skf "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
    ok "Ollama API responding at $OLLAMA_URL"

    # Installed models
    info "Installed models:"
    curl -skf "${OLLAMA_URL}/api/tags" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    models = data.get('models', [])
    for m in models:
        name = m.get('name', '?')
        size_gb = m.get('size', 0) / (1024**3)
        quant = m.get('details', {}).get('quantization_level', '?')
        params = m.get('details', {}).get('parameter_size', '?')
        print(f'    {name:<30} {size_gb:.1f}GB  quant={quant}  params={params}')
except: pass
" 2>/dev/null || info "  (could not parse model list)"

    # Loaded models (in VRAM)
    echo
    info "Currently loaded models (VRAM):"
    LOADED_RESPONSE="$(curl -skf "${OLLAMA_URL}/api/ps" 2>/dev/null || echo '{}')"
    echo "$LOADED_RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    models = data.get('models', [])
    if not models:
        print('    (none loaded)')
    for m in models:
        name = m.get('name', '?')
        size_vram = m.get('size_vram', 0) / (1024**3)
        size = m.get('size', 0) / (1024**3)
        expires = m.get('expires_at', '?')
        print(f'    {name:<30} VRAM: {size_vram:.2f}GB / Total: {size:.2f}GB  expires: {expires}')
except: print('    (could not parse)')
" 2>/dev/null

    json_set "ollama_api" "healthy"
else
    fail "Ollama API not responding at $OLLAMA_URL"
    json_set "ollama_api" "unreachable"
fi

# ---------------------------------------------------------------------------
# 7. Disk & I/O
# ---------------------------------------------------------------------------
section "Disk & I/O"

# NVMe / SSD detection
info "Block devices:"
lsblk -d -o NAME,SIZE,TYPE,ROTA,TRAN,MODEL 2>/dev/null | head -10 | while IFS= read -r line; do
    info "  $line"
done

# I/O Scheduler
echo
info "I/O Schedulers:"
for dev in /sys/block/nvme* /sys/block/sd*; do
    [[ -e "$dev/queue/scheduler" ]] || continue
    DEVNAME="$(basename "$dev")"
    SCHED="$(cat "$dev/queue/scheduler" 2>/dev/null)"
    info "  $DEVNAME: $SCHED"
    # NVMe should use 'none'
    if [[ "$DEVNAME" == nvme* ]] && ! echo "$SCHED" | grep -q '\[none\]'; then
        warn "$DEVNAME: NVMe should use 'none' scheduler (currently: $SCHED)"
    fi
done

# Ollama models directory
OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/.ollama/models}"
if [[ -d "$OLLAMA_MODELS" ]]; then
    MODELS_DISK="$(df -h "$OLLAMA_MODELS" 2>/dev/null | tail -1 | awk '{print $1, $2, $3, $4, $5}')"
    MODELS_SIZE="$(du -sh "$OLLAMA_MODELS" 2>/dev/null | awk '{print $1}' || echo '?')"
    info "Ollama models dir: $OLLAMA_MODELS"
    info "  Size: $MODELS_SIZE"
    info "  Disk: $MODELS_DISK"
fi

# Disk I/O stats (if iostat available)
if command -v iostat &>/dev/null; then
    echo
    info "Current I/O stats:"
    iostat -x 1 1 2>/dev/null | grep -E "^(Device|nvme|sd)" | head -5 | while IFS= read -r line; do
        info "  $line"
    done
fi

# ---------------------------------------------------------------------------
# 8. Docker Containers
# ---------------------------------------------------------------------------
section "Docker Containers"

if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    RUNNING="$(docker ps --format '{{.Names}}' 2>/dev/null | wc -l)"
    info "Running containers: $RUNNING"

    if [[ "$RUNNING" -gt 0 ]]; then
        echo
        info "Container resource usage:"
        docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.PIDs}}" 2>/dev/null | while IFS= read -r line; do
            info "  $line"
        done
    fi

    # Check for execution workers
    WORKERS="$(docker ps --filter "name=orc-execution" --format '{{.Names}} ({{.Status}})' 2>/dev/null)"
    if [[ -n "$WORKERS" ]]; then
        echo
        info "Execution workers:"
        echo "$WORKERS" | while IFS= read -r w; do
            info "  $w"
        done
    fi

    json_set "docker_containers_running" "$RUNNING"
else
    warn "Docker not accessible (permission issue or not running)"
fi

# ---------------------------------------------------------------------------
# 9. Network Ports
# ---------------------------------------------------------------------------
section "Network Ports (AI Services)"

declare -A EXPECTED_PORTS=(
    [11434]="Ollama"
    [8585]="Symbiont API"
    [8484]="RAG Service"
    [6380]="Redis (Execution)"
    [8123]="ClickHouse HTTPS"
    [9000]="ClickHouse Native"
    [3000]="Grafana"
    [4317]="OTel gRPC"
    [4318]="OTel HTTP"
)

for port in 11434 8585 8484 6380 8123 9000 3000 4317 4318; do
    SERVICE="${EXPECTED_PORTS[$port]}"
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        PROC="$(ss -tlnp 2>/dev/null | grep ":${port} " | grep -oP 'users:\(\("\K[^"]+' | head -1 || echo '?')"
        ok "Port $port ($SERVICE): LISTENING [$PROC]"
    else
        info "Port $port ($SERVICE): not listening"
    fi
done

# ---------------------------------------------------------------------------
# 10. Kernel Parameters
# ---------------------------------------------------------------------------
section "Kernel Parameters (Performance-Relevant)"

THP="$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo 'unknown')"
info "Transparent Huge Pages: $THP"
if echo "$THP" | grep -q '\[always\]'; then
    warn "THP=always can cause latency spikes (recommend: madvise)"
fi

OVERCOMMIT="$(cat /proc/sys/vm/overcommit_memory 2>/dev/null || echo '?')"
info "vm.overcommit_memory: $OVERCOMMIT"

MAX_MAP="$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo '?')"
info "vm.max_map_count: $MAX_MAP"
if [[ "$MAX_MAP" -lt 262144 ]] 2>/dev/null; then
    warn "vm.max_map_count=$MAX_MAP (low for ClickHouse/Ollama, recommend ≥262144)"
fi

# File descriptors
ULIMIT_N="$(ulimit -n 2>/dev/null || echo '?')"
info "Open files limit (ulimit -n): $ULIMIT_N"

# ---------------------------------------------------------------------------
# 11. Summary
# ---------------------------------------------------------------------------
section "Summary"

if [[ ${#ISSUES[@]} -eq 0 ]]; then
    echo -e "  ${GREEN}All checks passed — system looks healthy for AI workloads.${NC}"
else
    echo -e "  ${YELLOW}Issues found: ${#ISSUES[@]}${NC}"
    echo
    for issue in "${ISSUES[@]}"; do
        if [[ "$issue" == FAIL:* ]]; then
            echo -e "  ${RED}${issue}${NC}"
        else
            echo -e "  ${YELLOW}${issue}${NC}"
        fi
    done
fi

# ---------------------------------------------------------------------------
# 12. JSON Output
# ---------------------------------------------------------------------------
if [[ "$JSON_MODE" == "true" ]]; then
    BENCHMARKS_DIR="${PROJECT_ROOT}/benchmarks"
    mkdir -p "$BENCHMARKS_DIR"
    OUTPUT_FILE="${BENCHMARKS_DIR}/diagnostic_${TIMESTAMP}.json"

    # Build JSON
    {
        echo "{"
        echo "  \"timestamp\": \"$(date -Iseconds)\","
        echo "  \"issues_count\": ${#ISSUES[@]},"
        for key in "${!JSON_DATA[@]}"; do
            val="${JSON_DATA[$key]}"
            # Numeric or string?
            if [[ "$val" =~ ^[0-9]+\.?[0-9]*$ ]]; then
                echo "  \"$key\": $val,"
            else
                echo "  \"$key\": \"$(echo "$val" | sed 's/"/\\"/g')\","
            fi
        done
        echo "  \"issues\": ["
        for i in "${!ISSUES[@]}"; do
            COMMA=","
            [[ $i -eq $((${#ISSUES[@]} - 1)) ]] && COMMA=""
            echo "    \"$(echo "${ISSUES[$i]}" | sed 's/"/\\"/g')\"$COMMA"
        done
        echo "  ]"
        echo "}"
    } > "$OUTPUT_FILE"

    echo
    info "JSON output saved to: $OUTPUT_FILE"
fi

echo
echo -e "${BOLD}Diagnostic complete.${NC}"

# Exit code: 0 if no FAIL issues, 1 otherwise
for issue in "${ISSUES[@]}"; do
    [[ "$issue" == FAIL:* ]] && exit 1
done
exit 0
