#!/usr/bin/env bash
# Monitor de recursos durante rag sync
# Uso: bash scripts/monitor_rag.sh [intervalo_segundos]

set -euo pipefail

INTERVAL="${1:-3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAG_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="${RAG_DATA_DIR:-$RAG_ROOT/data}"

echo "╔══════════════════════════════════════════════════════╗"
echo "║  Monitor RAG — Ctrl+C para parar                    ║"
echo "║  Intervalo: ${INTERVAL}s                                     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

while true; do
    clear
    echo "══════════════ $(date '+%H:%M:%S') ══════════════"
    echo ""

    # RAM
    echo "── RAM ──"
    free -h | head -2
    echo ""

    # CPU
    echo "── CPU ──"
    top -bn1 | grep "Cpu(s)" | sed 's/^%//'
    echo ""

    # Disco
    echo "── Disco (partição data/) ──"
    df -h "$DATA_DIR" 2>/dev/null || df -h ~
    echo ""

    # Processos relevantes
    echo "── Processos RAG/Ollama/Graphify ──"
    ps aux | grep -E '(rag.sync|ollama|graphify|embed|qdrant)' | grep -v grep || echo "  (nenhum activo)"
    echo ""

    # GPU (se disponível)
    if command -v nvidia-smi &>/dev/null; then
        echo "── GPU ──"
        nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null || echo "  nvidia-smi indisponível"
        echo ""
    fi

    sleep "$INTERVAL"
done
