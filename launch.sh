#!/usr/bin/env bash
# ==============================================================================
# llmCode - Launch llm-dash Service (Linux & macOS)
# Auto-verifies paths in config/config.json, starts llm-dash daemon in background,
# and opens the browser.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1

VENV_PY="$SCRIPT_DIR/src/LLMServer/.venv/bin/python3"

# Run setup automatically if virtual environment is missing or broken
if [[ ! -x "$VENV_PY" ]] || ! "$VENV_PY" -c "import fastapi, uvicorn" 2>/dev/null; then
    echo "Environment not initialized or relocated. Running setup.sh first..."
    "$SCRIPT_DIR/setup.sh"
fi

# Sync config/config.json paths and launch llm-dash in background daemon mode
exec "$VENV_PY" "$SCRIPT_DIR/llmcode.py" dash --daemon "$@"
