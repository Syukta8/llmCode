#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$BASE_DIR/src/LLMServer/.venv"

echo "==================================================="
echo " Installing Aider in LLMServer isolated venv..."
echo "==================================================="

if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install aider-chat

mkdir -p "$HOME/.local/bin"
ln -sf "$VENV_DIR/bin/aider" "$HOME/.local/bin/aider"

echo ""
echo "Aider installed successfully in isolated venv:"
"$VENV_DIR/bin/aider" --version
echo "Symlinked to $HOME/.local/bin/aider"
