#!/usr/bin/env bash
# ==============================================================================
# llmCode - Unified Linux Installer & Setup Script
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== llmCode Setup (Linux) ==="

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Please install Python 3.10+ first."
    exit 1
fi

PYTHON_BIN="$(command -v python3)"
PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "Using Python $PY_VER ($PYTHON_BIN)"

# Check or create venv
VENV_DIR="$SCRIPT_DIR/src/LLMServer/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at $VENV_DIR..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"

# Install requirements
echo "Installing Python dependencies..."
"$VENV_PIP" install --upgrade pip -q
"$VENV_PIP" install -q -r "$SCRIPT_DIR/src/LLMServer/requirements.txt"

# Optional tool installation (Aider / OpenCode)
if [ "${1:-}" = "--with-tools" ] || [ "${INSTALL_TOOLS:-0}" = "1" ]; then
    echo "Installing optional coding agents (aider-chat)..."
    "$VENV_PIP" install -q aider-chat || echo "Note: aider-chat install skipped or failed."
fi

# Run setup binaries logic via llmcode.py
echo "Configuring engine binaries..."
"$VENV_PY" "$SCRIPT_DIR/llmcode.py" setup "$@"

echo "=== llmCode Setup Complete ==="
echo "Launch dashboard with: ./setup.sh run (or: ./src/LLMServer/.venv/bin/python3 llmcode.py dash)"

if [ "${1:-}" = "run" ] || [ "${1:-}" = "dash" ]; then
    exec "$VENV_PY" "$SCRIPT_DIR/llmcode.py" dash
fi
