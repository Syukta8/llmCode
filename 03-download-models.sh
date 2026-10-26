#!/usr/bin/env bash
set -euo pipefail
BASE="${LOCAL_AI_HOME:-$HOME/llmCode}"; DIR="$BASE/models/hauhaucs"; mkdir -p "$DIR"
which_model="${1:-4b}"
case "$which_model" in 4b|9b|both) ;; *) echo 'Usage: 03-download-models.sh [4b|9b|both]' >&2; exit 2;; esac
get_model(){
  local SIZE="$1" REPO="Qwen3.5-${1}-Uncensored-HauhauCS-Aggressive"
  local URL="https://huggingface.co/HauhauCS/${REPO}/resolve/main/${REPO}-Q4_K_M.gguf"
  local OUT="$DIR/qwen35-${SIZE,,}-q4km.gguf"
  echo "Downloading $REPO to $OUT"
  curl -fL -C - --retry 5 --retry-delay 3 -o "$OUT" "$URL"
  ls -lh "$OUT"; sha256sum "$OUT"
  echo 'Compare SHA-256 with the corresponding Hugging Face file page if integrity assurance is required.'
}
if [[ "$which_model" == 4b || "$which_model" == both ]]; then get_model 4B; fi
if [[ "$which_model" == 9b || "$which_model" == both ]]; then get_model 9B; fi
