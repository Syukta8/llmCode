#!/usr/bin/env bash
set -euo pipefail

BASE="${LOCAL_AI_HOME:-$HOME/llmCode}"
OC_CONFIG_DIR="$HOME/.config/opencode"

echo "=== Installing OpenCode CLI 2.0 ==="

# Check / install opencode CLI 2.0 via official installer
if command -v opencode >/dev/null 2>&1 && opencode --version 2>&1 | grep -q "v2"; then
  echo "OpenCode CLI 2.0 already installed: $(opencode --version)"
else
  echo "Installing OpenCode CLI 2.0..."
  curl -fsSL https://opencode.ai/v2/install | bash
fi

# Ensure ~/.opencode/bin is in PATH if installed there
if [[ -x "$HOME/.opencode/bin/opencode" && ":$PATH:" != *":$HOME/.opencode/bin:"* ]]; then
  echo "Adding ~/.opencode/bin to current session PATH"
  export PATH="$HOME/.opencode/bin:$PATH"
fi

# Configure OpenCode 2.0 for local llama.cpp
mkdir -p "$OC_CONFIG_DIR"
CONFIG_FILE="$OC_CONFIG_DIR/opencode.jsonc"

echo "Configuring $CONFIG_FILE for local llama.cpp (127.0.0.1:8080)..."
cat > "$CONFIG_FILE" <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "model": "llamacpp/default",
  "providers": {
    "llamacpp": {
      "name": "Local llama.cpp",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:8080/v1"
      },
      "models": {
        "default": {
          "name": "llama.cpp Active Model",
          "limit": {
            "context": 65536,
            "output": 4096
          }
        }
      }
    }
  }
}
EOF

CLI_SETTINGS="$OC_CONFIG_DIR/cli.json"
cat > "$CLI_SETTINGS" <<'EOF'
{
  "$schema": "https://opencode.ai/cli.json",
  "keybinds": {
    "prompt_submit": "return",
    "input_newline": "shift+return"
  }
}
EOF

echo "OpenCode 2.0 configuration written to $CONFIG_FILE and $CLI_SETTINGS"
echo "OpenCode CLI 2.0 setup complete."
