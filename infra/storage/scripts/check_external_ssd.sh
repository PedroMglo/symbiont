#!/usr/bin/env bash
set -euo pipefail

# check_external_ssd.sh — Validates the external SSD is ready for ai-local use.
# Exit 0 if valid, non-zero otherwise. Prints diagnostics to stderr.

# Resolve storage root from generated central env if not provided.
_AI_STORAGE_ROOT_ENV="${AI_STORAGE_ROOT:-}"
if [[ -z "$_AI_STORAGE_ROOT_ENV" ]]; then
  _PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
  _STORAGE_ENV="${_PROJECT_ROOT}/.env.storage.generated"
  if [[ -f "$_STORAGE_ENV" ]]; then
    _MODE="$(grep '^AI_LOCAL_STORAGE_MODE=' "$_STORAGE_ENV" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')" || true
    if [[ "$_MODE" == "external" ]]; then
      _AI_STORAGE_ROOT_ENV="$(grep '^AI_LOCAL_STORAGE_ROOT=' "$_STORAGE_ENV" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')" || true
    fi
  fi
fi
SSD_PATH="${1:-${_AI_STORAGE_ROOT_ENV:-}}"
EXPECTED_DEVICE="${AI_SSD_EXPECTED_DEVICE:-/dev/sda1}"
EXPECTED_FS="${AI_SSD_EXPECTED_FS:-ext4}"

die() { echo "FAIL: $*" >&2; exit 1; }
info() { echo "CHECK: $*" >&2; }

# --- Argument validation ---
[[ -z "$SSD_PATH" ]] && die "Usage: $0 <ssd_path>, set AI_STORAGE_ROOT, or run make infra."
[[ "$SSD_PATH" != /* ]] && die "SSD_PATH must be an absolute path: $SSD_PATH"
[[ "$SSD_PATH" == "none" || "$SSD_PATH" == "null" || "$SSD_PATH" == "local" ]] && die "SSD_PATH is set to '$SSD_PATH' (local mode). Nothing to check."

# --- Existence ---
info "Path exists: $SSD_PATH"
[[ -d "$SSD_PATH" ]] || die "Directory does not exist: $SSD_PATH"

# --- Mount check ---
MOUNT_SRC=$(findmnt -n -o SOURCE --target "$SSD_PATH" 2>/dev/null || true)
info "Mount source: ${MOUNT_SRC:-NONE}"
[[ -n "$MOUNT_SRC" ]] || die "No mount found for $SSD_PATH"
[[ "$MOUNT_SRC" == "$EXPECTED_DEVICE" ]] || die "Expected device $EXPECTED_DEVICE, got $MOUNT_SRC"

# --- Filesystem type ---
MOUNT_FS=$(findmnt -n -o FSTYPE --target "$SSD_PATH" 2>/dev/null || true)
info "Filesystem: ${MOUNT_FS:-UNKNOWN}"
[[ "$MOUNT_FS" == "$EXPECTED_FS" ]] || die "Expected $EXPECTED_FS filesystem, got $MOUNT_FS"

# --- Writability ---
info "Writable check..."
[[ -w "$SSD_PATH" ]] || die "Directory is not writable: $SSD_PATH"

# --- POSIX feature test (symlinks, permissions) ---
TEST_DIR="$SSD_PATH/.posix_test_$$"
cleanup() { rm -rf "$TEST_DIR" 2>/dev/null || true; }
trap cleanup EXIT

mkdir -p "$TEST_DIR"
echo "test" > "$TEST_DIR/file"
ln -s "$TEST_DIR/file" "$TEST_DIR/symlink" || die "Symlink creation failed"
chmod 600 "$TEST_DIR/file" || die "chmod failed"
[[ -L "$TEST_DIR/symlink" ]] || die "Symlink verification failed"
info "POSIX features: OK"

# --- Required subdirectories (Phase 1 — must exist before use) ---
REQUIRED_DIRS=(
  "data/models/gguf"
  "data/cache/hf"
  "data/audio"
  "data/graphify"
  "logs"
)

for dir in "${REQUIRED_DIRS[@]}"; do
  [[ -d "$SSD_PATH/$dir" ]] || die "Missing required directory: $SSD_PATH/$dir"
done
info "Required directories: OK"

# --- Optional subdirectories (Phase 2 — create if missing) ---
OPTIONAL_DIRS=(
  "data/docker-volumes/qdrant"
  "data/docker-volumes/clickhouse/data"
  "logs/clickhouse"
  "data/docker-volumes/grafana"
  "data/docker-volumes/langfuse-db"
  "data/docker-volumes/redis"
)

for dir in "${OPTIONAL_DIRS[@]}"; do
  if [[ ! -d "$SSD_PATH/$dir" ]]; then
    mkdir -p "$SSD_PATH/$dir" && info "Created optional dir: $dir"
  fi
done

echo "OK: External SSD at $SSD_PATH is valid and ready." >&2
exit 0
