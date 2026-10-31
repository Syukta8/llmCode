#!/usr/bin/env bash
# ==============================================================================
# llmCode - Unified Installer, Hardware Scanner & Uninstaller (Linux & macOS)
# Supports:
#   ./setup.sh              -> Full interactive setup (hardware.json, engines, venv agents, Continue)
#   ./setup.sh --uninstall  -> Complete uninstall of venv, engines, binaries, models, configs
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

OS_NAME="$(uname -s)"
VENV_DIR="$SCRIPT_DIR/src/LLMServer/.venv"
VENV_PY="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"

# Handle --uninstall before touching or creating .venv
IS_FULL=0
for arg in "$@"; do
    if [[ "$arg" == "--uninstall" ]]; then
        python3 "$SCRIPT_DIR/llmcode.py" setup "$@"
        exit 0
    elif [[ "$arg" == "--full" ]]; then
        IS_FULL=1
    fi
done

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

# Check for existing deployment and perform incremental update check
DEPLOY_STATE="$SCRIPT_DIR/config/deploy_state.json"
if [[ "$IS_FULL" -eq 0 ]] && [[ -f "$DEPLOY_STATE" ]]; then
    echo "Existing deployment detected. Running incremental update check..."
    "$VENV_PY" "$SCRIPT_DIR/llmcode.py" check-update "$@"
    exit 0
fi

# 3. Install core dashboard requirements
echo "Installing core dashboard dependencies..."
"$VENV_PIP" install --upgrade pip -q --no-warn-script-location
"$VENV_PIP" install -q --no-warn-script-location -r "$SCRIPT_DIR/src/LLMServer/requirements.txt"

# 4. Run hardware scan, engine compiler/downloader, venv CLI agents & Continue setup
"$VENV_PY" "$SCRIPT_DIR/llmcode.py" setup "$@"

echo ""
echo "=== llmCode Setup Complete ==="
echo "  Launch dashboard : ./launch.sh"
echo "  Stop services    : ./stop.sh"
echo "  Uninstall all    : ./setup.sh --uninstall"
