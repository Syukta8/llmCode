#!/usr/bin/env bash
set -euo pipefail
BASE="${LOCAL_AI_HOME:-$HOME/llmCode}"
MODEL="${1:-4b}"; LAYERS="${2:-}"; CTX="${3:-}"
case "$MODEL" in
  4b) LAYERS="${LAYERS:-8}"; CTX="${CTX:-8192}" ;;
  9b) LAYERS="${LAYERS:-4}"; CTX="${CTX:-4096}" ;;
  *) echo 'Usage: 04-serve-model.sh [4b|9b] [gpu_layers] [ctx_tokens]' >&2; exit 2 ;;
esac
GGUF="$BASE/models/hauhaucs/qwen35-$MODEL-q4km.gguf"
BIN="$BASE/src/llama.cpp/build-cuda/bin/llama-server"
[[ -s "$GGUF" ]] || { echo "Missing model: $GGUF" >&2; exit 1; }
[[ -x "$BIN" ]] || { echo "Missing binary: $BIN" >&2; exit 1; }
[[ "$LAYERS" =~ ^[0-9]+$ && "$CTX" =~ ^[0-9]+$ ]] || { echo 'Layers/context must be integers' >&2; exit 2; }
echo "Starting $MODEL with $LAYERS GPU layers and $CTX context tokens"
exec "$BIN" --model "$GGUF" --alias "hauhaucs-qwen35-$MODEL" \
  --host 127.0.0.1 --port 8080 --ctx-size "$CTX" --n-gpu-layers "$LAYERS" \
  --parallel 1 --jinja -fa on -ctk q8_0 -ctv q8_0
