#!/usr/bin/env bash
# backup_config.sh — Snapshot all AI infrastructure configuration for safe rollback.
#
# Creates: backups/{timestamp}/ with copies of all configuration files.
#
# Usage:
#   ./infra/systemd/scripts/backup_config.sh                # Create backup
#   ./infra/systemd/scripts/backup_config.sh --list         # List existing backups
#   ./infra/systemd/scripts/backup_config.sh --restore DIR  # Restore from backup (interactive)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
BACKUP_BASE="${PROJECT_ROOT}/backups"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${BACKUP_BASE}/${TIMESTAMP}"

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# --- List mode ---
if [[ "${1:-}" == "--list" ]]; then
    echo -e "${BOLD}Existing backups:${NC}"
    if [[ -d "$BACKUP_BASE" ]]; then
        ls -1d "$BACKUP_BASE"/*/ 2>/dev/null | while read -r d; do
            SIZE="$(du -sh "$d" 2>/dev/null | awk '{print $1}')"
            echo "  $(basename "$d")  ($SIZE)"
        done
    else
        echo "  (none)"
    fi
    exit 0
fi

# --- Restore mode ---
if [[ "${1:-}" == "--restore" ]]; then
    RESTORE_DIR="${2:-}"
    if [[ -z "$RESTORE_DIR" || ! -d "$RESTORE_DIR" ]]; then
        echo "Usage: $0 --restore <backup_directory>"
        echo "Available:"
        ls -1d "$BACKUP_BASE"/*/ 2>/dev/null || echo "  (none)"
        exit 1
    fi
    echo -e "${YELLOW}⚠  Restore is manual — review files in: $RESTORE_DIR${NC}"
    echo "  Files backed up:"
    find "$RESTORE_DIR" -type f | sed "s|$RESTORE_DIR/|  |"
    echo
    echo "  To restore config/orc/:"
    echo "    cp -r '$RESTORE_DIR/config/orc/' '${PROJECT_ROOT}/config/orc/'"
    echo
    echo "  To restore systemd override:"
    echo "    sudo cp '$RESTORE_DIR/systemd/override.conf' '/etc/systemd/system/ollama.service.d/override.conf'"
    echo "    sudo systemctl daemon-reload && sudo systemctl restart ollama"
    exit 0
fi

# --- Create backup ---
echo -e "${BOLD}Creating configuration backup...${NC}"
mkdir -p "$BACKUP_DIR"

# Project config files
for f in pyproject.toml .env; do
    [[ -f "${PROJECT_ROOT}/$f" ]] && cp "${PROJECT_ROOT}/$f" "${BACKUP_DIR}/$f"
done

# config/orc directory
CONFIG_ORC="${PROJECT_ROOT}/config/orc"
if [[ -d "$CONFIG_ORC" ]]; then
    mkdir -p "${BACKUP_DIR}/config/orc"
    cp "${CONFIG_ORC}"/*.toml "${BACKUP_DIR}/config/orc/" 2>/dev/null
fi

# Canonical infrastructure configs
INFRA_DOCKER="${PROJECT_ROOT}/infra/docker"
if [[ -d "$INFRA_DOCKER" ]]; then
    mkdir -p "${BACKUP_DIR}/infra/docker"
    cp -R "$INFRA_DOCKER"/compose "$INFRA_DOCKER"/otel "$INFRA_DOCKER"/grafana "${BACKUP_DIR}/infra/docker/" 2>/dev/null || true
fi

# Systemd Ollama override
SYSTEMD_DIR="/etc/systemd/system/ollama.service.d"
if [[ -d "$SYSTEMD_DIR" ]]; then
    mkdir -p "${BACKUP_DIR}/systemd"
    sudo cp "$SYSTEMD_DIR"/*.conf "${BACKUP_DIR}/systemd/" 2>/dev/null || true
    # Make readable by current user
    sudo chown -R "$(id -u):$(id -g)" "${BACKUP_DIR}/systemd/" 2>/dev/null || true
fi

# Docker daemon config
if [[ -f "/etc/docker/daemon.json" ]]; then
    mkdir -p "${BACKUP_DIR}/docker"
    sudo cp "/etc/docker/daemon.json" "${BACKUP_DIR}/docker/" 2>/dev/null || true
    sudo chown "$(id -u):$(id -g)" "${BACKUP_DIR}/docker/daemon.json" 2>/dev/null || true
fi

# Kernel parameters snapshot
{
    echo "# Kernel parameters snapshot — $(date)"
    echo "vm.swappiness = $(cat /proc/sys/vm/swappiness)"
    echo "vm.dirty_ratio = $(cat /proc/sys/vm/dirty_ratio)"
    echo "vm.dirty_background_ratio = $(cat /proc/sys/vm/dirty_background_ratio)"
    echo "vm.max_map_count = $(cat /proc/sys/vm/max_map_count)"
    echo "# THP: $(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo 'unknown')"
} > "${BACKUP_DIR}/kernel_params.txt"

# Ollama environment (from systemd)
systemctl show ollama --property=Environment 2>/dev/null > "${BACKUP_DIR}/ollama_env.txt" || true

# Summary
FILE_COUNT="$(find "$BACKUP_DIR" -type f | wc -l)"
TOTAL_SIZE="$(du -sh "$BACKUP_DIR" | awk '{print $1}')"

echo -e "${GREEN}✓${NC} Backup created: ${BACKUP_DIR}"
echo "  Files: $FILE_COUNT"
echo "  Size: $TOTAL_SIZE"
echo
echo "  To restore: $0 --restore $BACKUP_DIR"
