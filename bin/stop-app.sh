#!/usr/bin/env bash
set -euo pipefail

echo "=== Stopping llmCode ==="

# Stop dashboard uvicorn process
if pgrep -f "uvicorn app:app" >/dev/null 2>&1; then
  echo "Stopping llmCode dashboard server..."
  pkill -f "uvicorn app:app" || true
else
  echo "llmCode dashboard server not running."
fi

# Stop llama.cpp inference server if running
if pgrep -f "llama-server" >/dev/null 2>&1; then
  echo "Stopping llama.cpp inference server..."
  pkill -f "llama-server" || true
else
  echo "llama.cpp server not running."
fi

# Clean up PID files
rm -f "$HOME/.local/share/llmCode/llmserver.pid" "$HOME/.local/share/localllm-control-center/llmserver.pid" 2>/dev/null || true

echo "All llmCode processes stopped."
