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

PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
                PYTHON_BIN="$(command -v "$candidate")"
                break
            fi
        fi
    done
fi

if [ -z "$PYTHON_BIN" ]; then
    fail "Python 3.11+ not found. Install a supported Python runtime, then rerun make setup."
    exit 1
fi

PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.minor)')"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    fail "Python $PY_VERSION found at $PYTHON_BIN; Python 3.11+ is required."
    exit 1
fi
ok "Python $PY_VERSION ($PYTHON_BIN)"

if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
    fail "Python venv module is unavailable for $PYTHON_BIN. Install your distro's Python 3.11+ venv package."
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
    "$PYTHON_BIN" -m venv "$VENV_DIR"
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
