#!/usr/bin/env bash
# tune_system.sh — Linux kernel & system tuning for AI workloads.
#
# Optimizes: swap behavior, I/O, memory management, THP.
# All changes are reversible via: ./scripts/rollback/rollback_system.sh
#
# Usage:
#   ./scripts/tune_system.sh          # Show recommendations
#   ./scripts/tune_system.sh --apply  # Apply with confirmation

set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SYSCTL_FILE="/etc/sysctl.d/99-ai-symbiont.conf"

echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  System Tuning — AI Workloads (Ollama + Docker)${NC}"
echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
echo

# ---------------------------------------------------------------------------
# 1. Current State
# ---------------------------------------------------------------------------
echo -e "${BOLD}── Current Kernel Parameters ──${NC}"
echo

CURRENT_SWAPPINESS="$(cat /proc/sys/vm/swappiness)"
CURRENT_DIRTY_RATIO="$(cat /proc/sys/vm/dirty_ratio)"
CURRENT_DIRTY_BG="$(cat /proc/sys/vm/dirty_background_ratio)"
CURRENT_MAX_MAP="$(cat /proc/sys/vm/max_map_count)"
CURRENT_THP="$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo 'unknown')"

printf "  %-30s %s\n" "vm.swappiness:" "$CURRENT_SWAPPINESS"
printf "  %-30s %s\n" "vm.dirty_ratio:" "$CURRENT_DIRTY_RATIO"
printf "  %-30s %s\n" "vm.dirty_background_ratio:" "$CURRENT_DIRTY_BG"
printf "  %-30s %s\n" "vm.max_map_count:" "$CURRENT_MAX_MAP"
printf "  %-30s %s\n" "transparent_hugepages:" "$CURRENT_THP"

echo

# ---------------------------------------------------------------------------
# 2. Recommendations
# ---------------------------------------------------------------------------
echo -e "${BOLD}── Recommended Settings ──${NC}"
echo

declare -A RECOMMENDED=(
    ["vm.swappiness"]="10"
    ["vm.dirty_ratio"]="15"
    ["vm.dirty_background_ratio"]="5"
    ["vm.max_map_count"]="262144"
)

declare -A DESCRIPTIONS=(
    ["vm.swappiness"]="Reduce swap aggressiveness — keeps model data in RAM, avoids I/O stalls"
    ["vm.dirty_ratio"]="Flush dirty pages sooner — reduces write latency spikes during inference"
    ["vm.dirty_background_ratio"]="Start background writeback earlier — smoother I/O during heavy loads"
    ["vm.max_map_count"]="Required by ClickHouse & large model mmap — prevents ENOMEM on model load"
)

declare -A CURRENT_VALUES=(
    ["vm.swappiness"]="$CURRENT_SWAPPINESS"
    ["vm.dirty_ratio"]="$CURRENT_DIRTY_RATIO"
    ["vm.dirty_background_ratio"]="$CURRENT_DIRTY_BG"
    ["vm.max_map_count"]="$CURRENT_MAX_MAP"
)

CHANGES_NEEDED=false
for key in "vm.swappiness" "vm.dirty_ratio" "vm.dirty_background_ratio" "vm.max_map_count"; do
    current="${CURRENT_VALUES[$key]}"
    recommended="${RECOMMENDED[$key]}"
    desc="${DESCRIPTIONS[$key]}"

    if [[ "$current" == "$recommended" ]]; then
        echo -e "  ${GREEN}✓${NC} ${key} = ${recommended}"
    else
        echo -e "  ${YELLOW}→${NC} ${key} = ${recommended}  (currently: ${current})"
        CHANGES_NEEDED=true
    fi
    echo "    ${desc}"
    echo
done

# THP recommendation
echo -e "  ${BOLD}Transparent Huge Pages (THP):${NC}"
if echo "$CURRENT_THP" | grep -q '\[madvise\]'; then
    echo -e "  ${GREEN}✓${NC} THP = madvise"
else
    echo -e "  ${YELLOW}→${NC} THP = madvise  (currently: $CURRENT_THP)"
    CHANGES_NEEDED=true
fi
echo "    Use madvise — avoids latency spikes from compaction while allowing explicit hugepage use"
echo

# ---------------------------------------------------------------------------
# 3. NVMe I/O Scheduler
# ---------------------------------------------------------------------------
echo -e "${BOLD}── I/O Scheduler ──${NC}"
echo

for dev in /sys/block/nvme*; do
    [[ -e "$dev/queue/scheduler" ]] || continue
    DEVNAME="$(basename "$dev")"
    SCHED="$(cat "$dev/queue/scheduler")"
    if echo "$SCHED" | grep -q '\[none\]'; then
        echo -e "  ${GREEN}✓${NC} $DEVNAME: [none] (optimal for NVMe)"
    else
        echo -e "  ${YELLOW}→${NC} $DEVNAME: should be 'none' (currently: $SCHED)"
        CHANGES_NEEDED=true
    fi
done

for dev in /sys/block/sd*; do
    [[ -e "$dev/queue/scheduler" ]] || continue
    DEVNAME="$(basename "$dev")"
    SCHED="$(cat "$dev/queue/scheduler")"
    if echo "$SCHED" | grep -q '\[mq-deadline\]'; then
        echo -e "  ${GREEN}✓${NC} $DEVNAME: [mq-deadline] (good for SATA SSD)"
    else
        echo -e "  ${YELLOW}ℹ${NC} $DEVNAME: $SCHED (mq-deadline recommended for SATA)"
    fi
done

echo

# ---------------------------------------------------------------------------
# 4. Apply mode
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--apply" ]]; then
    if [[ "$CHANGES_NEEDED" == "false" ]]; then
        echo -e "${GREEN}✓ All settings already optimal — nothing to apply.${NC}"
        exit 0
    fi

    echo -e "${BOLD}── Applying Configuration ──${NC}"
    echo
    echo -e "${YELLOW}⚠  This will:${NC}"
    echo "  1. Create/overwrite $SYSCTL_FILE"
    echo "  2. Apply sysctl changes immediately"
    echo "  3. Set THP to madvise"
    echo "  4. Set NVMe scheduler to none (if applicable)"
    echo
    echo "  Rollback: ./scripts/rollback/rollback_system.sh"
    echo
    read -rp "  Continue? (y/N) " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo "  Aborted."
        exit 0
    fi

    # Create sysctl config
    echo "  Creating $SYSCTL_FILE..."
    sudo tee "$SYSCTL_FILE" > /dev/null << 'EOF'
# AI Symbiont — System tuning for LLM inference workloads
# Optimized for: 32GB RAM, NVIDIA RTX 4060 8GB, Ollama + Docker
# Rollback: ./scripts/rollback/rollback_system.sh

# Reduce swap aggressiveness — keep model weights in RAM
vm.swappiness = 10

# Flush dirty pages sooner — lower write latency during inference
vm.dirty_ratio = 15
vm.dirty_background_ratio = 5

# Required for ClickHouse and large model mmap operations
vm.max_map_count = 262144
EOF

    # Apply immediately
    echo "  Applying sysctl..."
    sudo sysctl -p "$SYSCTL_FILE"

    # THP
    if [[ -f /sys/kernel/mm/transparent_hugepage/enabled ]]; then
        echo "  Setting THP to madvise..."
        echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled >/dev/null
    fi

    # NVMe scheduler
    for dev in /sys/block/nvme*; do
        [[ -e "$dev/queue/scheduler" ]] || continue
        DEVNAME="$(basename "$dev")"
        if ! cat "$dev/queue/scheduler" | grep -q '\[none\]'; then
            echo "  Setting $DEVNAME scheduler to none..."
            echo none | sudo tee "$dev/queue/scheduler" >/dev/null
        fi
    done

    echo
    echo -e "${GREEN}✓ System tuning applied.${NC}"
    echo
    echo "  Verify with: ./scripts/diagnose_system.sh"
    echo "  Rollback:    ./scripts/rollback/rollback_system.sh"
else
    if [[ "$CHANGES_NEEDED" == "true" ]]; then
        echo -e "${YELLOW}To apply: $0 --apply${NC}"
    fi
fi
