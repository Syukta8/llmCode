#!/usr/bin/env bash
# ==============================================================================
# llmCode - Unified Installer & Setup Script (Linux & macOS)
# Sets up Python venv, compiles/downloads backend engines in src/ & bin/,
# installs CLI agents (Aider, OpenCode), and initializes config/config.json.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

OS_NAME="$(uname -s)"
VENV_DIR="$SCRIPT_DIR/src/LLMServer/.venv"
VENV_PY="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"

echo "=== llmCode Full Setup ($OS_NAME) ==="

# 1. Check Python 3.10+
if ! command -v python3 &>/dev/null; then
    echo "[MISSING] python3 is not installed."
    read -r -p "Permit auto-install of Python 3 now? [y/N]: " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        if [[ "$OS_NAME" == "Darwin" ]]; then
            brew install python
        elif command -v apt-get &>/dev/null; then
            sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y python3 python3-pip
        elif command -v pacman &>/dev/null; then
            sudo pacman -S --noconfirm python python-pip
        fi
    else
        echo "Manual guidance: Install Python 3.10+ via your system package manager or https://www.python.org"
        exit 1
    fi
fi

PYTHON_BIN="$(command -v python3)"
PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "Using Python $PY_VER ($PYTHON_BIN)"

# 2. Check or rebuild virtualenv
if [[ -d "$VENV_DIR" ]]; then
    if ! "$VENV_PY" -c "import sys; sys.exit(0)" 2>/dev/null; then
        echo "Rebuilding relocated/corrupted virtual environment at $VENV_DIR..."
        rm -rf "$VENV_DIR"
    fi
fi

if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating virtual environment at $VENV_DIR..."
    if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
        echo "[MISSING] python3-venv package required."
        if command -v apt-get &>/dev/null; then
            read -r -p "Permit auto-install of python3-venv via apt? [y/N]: " ans
            if [[ "$ans" =~ ^[Yy]$ ]]; then
                sudo apt-get update && sudo apt-get install -y python3-venv
                "$PYTHON_BIN" -m venv "$VENV_DIR"
            else
                echo "Run manually: sudo apt-get install -y python3-venv"
                exit 1
            fi
        else
            exit 1
        fi
    fi
fi

# 3. Install core dashboard requirements
echo "Installing core dashboard dependencies..."
"$VENV_PIP" install --upgrade pip -q
"$VENV_PIP" install -q -r "$SCRIPT_DIR/src/LLMServer/requirements.txt"

# 4. Run interactive backend compiler & CLI agent installer
"$VENV_PY" "$SCRIPT_DIR/llmcode.py" setup "$@"

echo ""
echo "=== llmCode Setup Complete ==="
echo "  Launch dashboard : ./launch.sh"
echo "  Stop services    : ./stop.sh"
