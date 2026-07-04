#!/usr/bin/env bash
# =============================================================================
# Download GGUF models for llama-cpp serving backend
# =============================================================================
# Usage: ./infra/docker/scripts/download-llm-models.sh [--all|--aux|--fast|--micro]
#
# Downloads quantized GGUF models from Hugging Face for CPU inference.
# Models are stored in LLM_MODELS_DIR, or .local/data/models/gguf when unset.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MODELS_DIR="${LLM_MODELS_DIR:-$PROJECT_ROOT/.local/data/models/gguf}"

# Model URLs (Hugging Face direct download — xet protocol)
# Using Qwen3 GGUF from official Qwen repo
QWEN3_4B_URL="https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf"
QWEN3_1_7B_URL="https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q8_0.gguf"
QWEN3_0_6B_URL="https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/resolve/main/qwen3-0.6b-q8_0.gguf"

# Model filenames expected by docker-compose
QWEN3_4B_FILE="Qwen3-4B-Q4_K_M.gguf"
QWEN3_1_7B_FILE="Qwen3-1.7B-Q8_0.gguf"
QWEN3_0_6B_FILE="qwen3-0.6b-q8_0.gguf"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

download_model() {
    local url="$1"
    local filename="$2"
    local target="$MODELS_DIR/$filename"

    if [[ -f "$target" ]]; then
        local size
        size=$(stat --format=%s "$target" 2>/dev/null || stat -f%z "$target" 2>/dev/null)
        if [[ "$size" -gt 100000000 ]]; then  # > 100MB = likely valid
            log_ok "$filename already exists ($(numfmt --to=iec "$size" 2>/dev/null || echo "${size}B"))"
            return 0
        fi
        log_warn "$filename exists but seems incomplete, re-downloading..."
    fi

    log_info "Downloading $filename..."
    if command -v wget &>/dev/null; then
        wget -q --show-progress -O "$target" "$url"
    elif command -v curl &>/dev/null; then
        curl -L --progress-bar -o "$target" "$url"
    else
        log_error "Neither wget nor curl found. Install one and retry."
        return 1
    fi

    log_ok "$filename downloaded ($(du -h "$target" | cut -f1))"
}

download_aux() {
    log_info "Downloading auxiliary model: Qwen3:4B (Q4_K_M) ..."
    download_model "$QWEN3_4B_URL" "$QWEN3_4B_FILE"
}

download_fast() {
    log_info "Downloading fast classifier: Qwen3:1.7B (Q5_K_M) ..."
    download_model "$QWEN3_1_7B_URL" "$QWEN3_1_7B_FILE"
}

download_micro() {
    log_info "Downloading micro model: Qwen3:0.6B (Q8_0) ..."
    download_model "$QWEN3_0_6B_URL" "$QWEN3_0_6B_FILE"
}

show_usage() {
    echo "Usage: $0 [--all|--aux|--fast|--micro]"
    echo ""
    echo "Options:"
    echo "  --all    Download all models (default)"
    echo "  --aux    Download Qwen3:4B (auxiliary agents)"
    echo "  --fast   Download Qwen3:1.7B (classifiers)"
    echo "  --micro  Download Qwen3:0.6B (micro-classifier)"
    echo ""
    echo "Models dir: $MODELS_DIR"
}

main() {
    mkdir -p "$MODELS_DIR"

    local mode="${1:---all}"

    case "$mode" in
        --all)
            log_info "Downloading all GGUF models to $MODELS_DIR"
            echo ""
            download_aux
            download_fast
            download_micro
            ;;
        --aux)   download_aux ;;
        --fast)  download_fast ;;
        --micro) download_micro ;;
        --help|-h)
            show_usage
            exit 0
            ;;
        *)
            log_error "Unknown option: $mode"
            show_usage
            exit 1
            ;;
    esac

    echo ""
    log_ok "All downloads complete. Models directory:"
    ls -lh "$MODELS_DIR"/*.gguf 2>/dev/null || log_warn "No .gguf files found"
    echo ""
    log_info "Start the llama-cpp backends with:"
    echo "  docker compose -f compose.yml --profile llm up -d"
}

main "$@"
