#!/usr/bin/env bash
# rollback_system.sh — Restore default kernel parameters.
#
# Reverts vm.swappiness, dirty_ratio, THP to Ubuntu/Fedora defaults.

set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SYSCTL_FILE="/etc/sysctl.d/99-ai-symbiont.conf"

echo -e "${BOLD}═══ System Parameters Rollback ═══${NC}"
echo

echo -e "${YELLOW}This will:${NC}"
echo "  1. Remove $SYSCTL_FILE (if exists)"
echo "  2. Restore default vm.swappiness (60)"
echo "  3. Restore default vm.dirty_ratio (20)"
echo "  4. Restore default vm.dirty_background_ratio (10)"
echo "  5. Restore THP to system default"
echo
read -rp "  Continue? (y/N) " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "  Aborted."
    exit 0
fi

# Remove sysctl config
if [[ -f "$SYSCTL_FILE" ]]; then
    echo "  Removing $SYSCTL_FILE..."
    sudo rm -f "$SYSCTL_FILE"
fi

# Restore defaults immediately
echo "  Setting vm.swappiness=60..."
sudo sysctl -w vm.swappiness=60 >/dev/null

echo "  Setting vm.dirty_ratio=20..."
sudo sysctl -w vm.dirty_ratio=20 >/dev/null

echo "  Setting vm.dirty_background_ratio=10..."
sudo sysctl -w vm.dirty_background_ratio=10 >/dev/null

# Restore THP (system default is usually 'always' or 'madvise')
if [[ -f /sys/kernel/mm/transparent_hugepage/enabled ]]; then
    echo "  Restoring THP to 'always'..."
    echo always | sudo tee /sys/kernel/mm/transparent_hugepage/enabled >/dev/null
fi

echo
echo -e "${GREEN}✓${NC} System parameters restored to defaults."
echo
echo "  Current values:"
echo "    vm.swappiness = $(cat /proc/sys/vm/swappiness)"
echo "    vm.dirty_ratio = $(cat /proc/sys/vm/dirty_ratio)"
echo "    vm.dirty_background_ratio = $(cat /proc/sys/vm/dirty_background_ratio)"
echo "    THP = $(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo 'unknown')"
