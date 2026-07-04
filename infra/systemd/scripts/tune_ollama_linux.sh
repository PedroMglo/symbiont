#!/usr/bin/env bash
# Compatibility wrapper for generated ai-local Ollama host config.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
OUTPUT_DIR="${ROOT_DIR}/.local/generated/ollama-host"

cd "${ROOT_DIR}"

python -m config.resolver --write-ollama-host-config "${OUTPUT_DIR}"

echo
echo "Generated Ollama systemd drop-in:"
sed 's/^/  /' "${OUTPUT_DIR}/90-ai-local.conf"
echo

if [[ "${1:-}" == "--apply" ]]; then
  sh "${OUTPUT_DIR}/apply-ollama-systemd.sh"
else
  echo "Apply with:"
  echo "  make ollama-host-apply"
fi
