#!/usr/bin/env bash
# rollback_ollama.sh — Restore default Ollama systemd configuration.
#
# Removes performance tuning override and restarts Ollama with defaults.

set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

OVERRIDE_DIR="/etc/systemd/system/ollama.service.d"

echo -e "${BOLD}═══ Ollama Rollback ═══${NC}"
echo

if [[ ! -d "$OVERRIDE_DIR" ]]; then
    echo -e "${GREEN}✓${NC} No override directory found — Ollama is using defaults."
    exit 0
fi

echo "Current overrides:"
for f in "$OVERRIDE_DIR"/*.conf; do
    [[ -f "$f" ]] || continue
    echo "  $(basename "$f"):"
    cat "$f" | sed 's/^/    /'
done
echo

echo -e "${YELLOW}This will:${NC}"
echo "  1. Remove all files in $OVERRIDE_DIR"
echo "  2. Reload systemd daemon"
echo "  3. Restart Ollama service"
echo
read -rp "  Continue? (y/N) " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "  Aborted."
    exit 0
fi

echo
echo "  Removing override directory..."
sudo rm -rf "$OVERRIDE_DIR"

echo "  Reloading systemd..."
sudo systemctl daemon-reload

echo "  Restarting Ollama..."
sudo systemctl restart ollama

sleep 2

if systemctl is-active --quiet ollama; then
    echo -e "  ${GREEN}✓${NC} Ollama restarted with default configuration."
    echo
    echo "  Current environment:"
    systemctl show ollama --property=Environment | sed 's/^/    /'
else
    echo -e "  ${RED}✗${NC} Ollama failed to start — check: journalctl -u ollama -n 20"
fi
