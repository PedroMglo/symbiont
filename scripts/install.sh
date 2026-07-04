#!/usr/bin/env bash
# =============================================================================
# ai-local user installer
# Creates the root virtualenv and installs only runtime/editable packages needed
# for local commands, generated config, storage checks and the @ alias.
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok() { printf "${GREEN}OK${NC} %s\n" "$1"; }
fail() { printf "${RED}FAIL${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}WARN${NC} %s\n" "$1"; }

echo "ai-local user install"
echo

if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found. Install Python 3.11+."
    exit 1
fi

PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="$(python3 -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$(python3 -c 'import sys; print(sys.version_info.minor)')"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    fail "Python $PY_VERSION found; Python 3.11+ is required."
    exit 1
fi
ok "Python $PY_VERSION"

if ! python3 -m venv --help >/dev/null 2>&1; then
    fail "python3 venv module is unavailable. Install your distro's python venv package."
    exit 1
fi
ok "python venv module"

if ! command -v git >/dev/null 2>&1; then
    fail "git not found."
    exit 1
fi
ok "$(git --version)"

VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    ok "Created $VENV_DIR"
else
    ok "$VENV_DIR already exists"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip --quiet
python -m pip install -e . -e storage_guardian/ --quiet
ok "Installed ai-local and storage_guardian"

if [ -f obsidian-rag/pyproject.toml ]; then
    python -m pip install -e obsidian-rag/ --quiet
    ok "Installed obsidian-rag"
else
    warn "obsidian-rag package is missing from the mono-repo checkout"
fi

echo
echo "Next steps:"
echo "  make aliases"
echo "  make infra"
echo "  make up"
echo "  make verify-live"
