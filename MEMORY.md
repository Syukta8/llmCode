# MEMORY.md - llmCode

## Stack & Architecture
- **Platforms**: Windows 10/11, Linux (Ubuntu/Debian/Fedora/Arch), macOS (Apple Silicon Metal / Intel).
- **Root Scripts**:
  - `setup.sh` / `setup.bat`: Full interactive environment setup, prerequisite check + auto-install permission prompt, per-engine (`llama.cpp`, `koboldcpp`, `ollama`) source compilation in `src/` or prebuilt binary download in `bin/`, and CLI agent (`aider`, `opencode`) installation.
  - `launch.sh` / `launch.bat`: Auto-verifies and heals paths in `config/config.json`, launches `llm-dash` (FastAPI/Uvicorn) as background daemon (`config/llmserver.pid`), waits for health check, and opens browser.
  - `stop.sh` / `stop.bat`: Stops `llm-dash` daemon and any running inference backend (`llama-server`, `koboldcpp`, `ollama`).
- **Database (`config/config.json`)**: Single JSON source of truth storing `paths`, `binaries`, `agents`, `settings`, and `downloads`. Auto-healed via `sync_config()` in `src/common/paths.py`.

## Decisions Log
- **2026-10-26**: Consolidated all runtime state into `config/config.json`, removed redundant `bin/launch-app.*`, `bin/stop-app.*`, `src/LLMServer/run.sh`, and `llama-commit.txt`, added full macOS support (`Terminal.app`/`iTerm.app`, `sysctl`/`vm_stat`, Metal `-DGGML_METAL=ON`), and rewrote `README.md` with per-platform setup instructions.
