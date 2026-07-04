#!/usr/bin/env bash
# rollback_docker.sh — Remove AI infrastructure resource limits from Docker compose files.
#
# Restores compose files to pre-optimization state using git.

set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

echo -e "${BOLD}═══ Docker Configuration Rollback ═══${NC}"
echo

echo -e "${YELLOW}This will restore Docker-related files to their git HEAD state:${NC}"
echo "  - infra/docker/compose/*.yml"
echo "  - infra/docker/images/**"
echo "  - infra/docker/otel/**"
echo "  - infra/docker/grafana/**"
echo

echo "Modified Docker files:"
cd "$PROJECT_ROOT"
git diff --name-only -- '*.yml' '*docker*' '*compose*' 2>/dev/null | sed 's/^/  /' || echo "  (none detected)"
echo

read -rp "  Restore these files from git? (y/N) " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "  Aborted."
    exit 0
fi

# Restore from git
FILES_RESTORED=0
for pattern in "infra/docker/compose" "infra/docker/images" "infra/docker/otel" "infra/docker/grafana"; do
    if git diff --quiet -- "$pattern" 2>/dev/null; then
        continue
    fi
    echo "  Restoring: $pattern"
    git checkout HEAD -- "$pattern" 2>/dev/null && ((FILES_RESTORED++)) || true
done

echo
if [[ $FILES_RESTORED -gt 0 ]]; then
    echo -e "${GREEN}✓${NC} Restored $FILES_RESTORED files to git HEAD."
    echo "  Restart affected services: docker compose restart"
else
    echo -e "${GREEN}✓${NC} No Docker files needed restoration."
fi
