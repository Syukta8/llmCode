#!/usr/bin/env bash
# ==============================================================================
# llmCode - Stop llm-dash & Inference Engine Services (Linux & macOS)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PY="$SCRIPT_DIR/src/LLMServer/.venv/bin/python3"
PYTHON_CMD="python3"
if [[ -x "$VENV_PY" ]]; then
    PYTHON_CMD="$VENV_PY"
fi

echo "=== Stopping llmCode Services ==="
"$PYTHON_CMD" "$SCRIPT_DIR/llmcode.py" stop || true

# Clean up any remaining PID files
rm -f "$SCRIPT_DIR/config/llmserver.pid" "$HOME/.local/share/llmCode/llmserver.pid" 2>/dev/null || true
echo "All llmCode services stopped."
