#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$REPO_ROOT/src/LLMServer"
VENV_DIR="$SERVER_DIR/.venv"
LOG_DIR="$HOME/.local/share/llmCode"
PID_FILE="$LOG_DIR/llmserver.pid"
APP_PORT="${LLAMA_PORT_DASH:-7860}"
APP_URL="http://127.0.0.1:$APP_PORT"

mkdir -p "$LOG_DIR"

# Check if dashboard already running
is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  # Fallback: check port response
  if curl -sf --max-time 1 "$APP_URL/api/status" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# If already running, open browser and exit cleanly
if is_running; then
  echo "llm-dash server already running. Opening browser..."
  xdg-open "$APP_URL" 2>/dev/null || sensible-browser "$APP_URL" 2>/dev/null || true
  exit 0
fi

# Ensure clean venv exists and is functional
if [[ -d "$VENV_DIR" ]]; then
  # Validate that venv's python works and matches current location
  if ! "$VENV_DIR/bin/python3" -c "import sys; sys.exit(0)" 2>/dev/null; then
    echo "Stale or corrupted virtualenv detected. Rebuilding $VENV_DIR..."
    rm -rf "$VENV_DIR"
  fi
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating virtual environment in $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
fi

# Ensure requirements installed using venv pip directly (bypasses PEP 668 system checks)
"$VENV_DIR/bin/pip" install -q -r "$SERVER_DIR/requirements.txt"

# Launch uvicorn daemon in background
echo "Starting llm-dash background server on :$APP_PORT..."
nohup "$VENV_DIR/bin/uvicorn" app:app \
  --app-dir "$SERVER_DIR" \
  --host 127.0.0.1 \
  --port "$APP_PORT" \
  > "$LOG_DIR/llmserver-daemon.log" 2>&1 </dev/null &

NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# Wait up to 10 seconds for server health check
for i in {1..20}; do
  if curl -sf --max-time 1 "$APP_URL/api/status" >/dev/null 2>&1; then
    echo "Server ready at $APP_URL"
    break
  fi
  sleep 0.5
done

# Auto-open browser
xdg-open "$APP_URL" 2>/dev/null || sensible-browser "$APP_URL" 2>/dev/null || true
