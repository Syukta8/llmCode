# MEMORY.md - llmCode

## Stack & Architecture
- **Platforms**: Windows 10/11, Linux (Ubuntu/Debian/Fedora/Arch), macOS (Apple Silicon Metal / Intel).
- **Root Scripts**:
  - `setup.sh` / `setup.bat`: Upfront hardware & package scanner (`config/hardware.json`), interactive prerequisite check + auto-install permission prompt, per-engine (`llama.cpp`, `koboldcpp`, `ollama`) hardware-tuned compilation in `src/` or prebuilt binary download in `bin/`, venv-scoped CLI agent (`aider`, `opencode`) installation in `src/LLMServer/.venv`, VS Code Continue auto-setup (`llamacpp`, `koboldcpp`, `ollama`), and `--uninstall` flag for complete removal.
  - `launch.sh` / `launch.bat`: Auto-verifies and heals paths in `config/config.json`, launches `llm-dash` (FastAPI/Uvicorn) as background daemon (`config/llmserver.pid`), waits for health check, and opens browser.
  - `stop.sh` / `stop.bat`: Stops `llm-dash` daemon and any running inference backend (`llama-server`, `koboldcpp`, `ollama`).
- **Database (`config/config.json` & `config/hardware.json`)**:
  - `config/hardware.json`: Stores CPU cores, RAM, GPU VRAM/compute capability/driver, installed package paths, `build_config` (CMake/Make flags, `CMAKE_CUDA_ARCHITECTURES`, parallel jobs), and `runtime_defaults` (auto-tuned layers, context, threads, batch).
  - `config/config.json`: Single JSON source of truth storing `paths`, `binaries`, `agents`, `settings`, and `downloads`. Auto-healed via `sync_config()` in `src/common/paths.py`.
- **Standard Backend Aliases**:
  - `llama.cpp` -> `llamacpp`
  - `koboldcpp` -> `koboldcpp`
  - `ollama` -> `ollama`

## Decisions Log
- **2026-10-26**: Consolidated all runtime state into `config/config.json`, removed redundant `bin/launch-app.*`, `bin/stop-app.*`, `src/LLMServer/run.sh`, and `llama-commit.txt`, added full macOS support (`Terminal.app`/`iTerm.app`, `sysctl`/`vm_stat`, Metal `-DGGML_METAL=ON`), and rewrote `README.md` with per-platform setup instructions.
- **2026-10-26 (Windows Fix)**: Raised `Deploy` context ceiling to 1M (`1048576`), added custom FastAPI `RequestValidationError` & `HTTPException` handlers that write directly to `logs/llama-server.log`, fixed Windows prebuilt DLL discovery (`bin/windows/llama-server.exe`), and added Engine vs Dashboard log selector in UI.
- **2026-10-27**: Added `config/hardware.json` upfront hardware & package scanner, auto-adjusted compilation flags and runtime defaults from `hardware.json`, scoped both `aider` and `opencode` inside `src/LLMServer/.venv`, standardized backend aliases (`llamacpp`, `koboldcpp`, `ollama`) across Continue (`~/.continue/config.yaml` / `config.json`), Aider, and OpenCode (`opencode.json`), and implemented `--uninstall` flag in `setup.sh` / `setup.bat` / `llmcode.py`.
- **2026-10-27 (RoPE, YaRN & Workspace RAG)**: Added Context Extension presets (`none`, `linear_2x`, `yarn_2x`, `yarn_4x`, `yarn_8x`) with automatic UI context size scaling and backend CLI flag generation (`--rope-scaling`/`--rope-scale`/`--yarn-ext-factor` for `llama.cpp`, `--ropeconfig` for `koboldcpp`). Added Workspace RAG (`src/common/rag.py`, `POST /api/rag/index`, `POST /api/rag/query`) plus `--embeddings --pooling mean` backend flags and automatic `config/rag_context.md` injection into Aider (`--read`) and OpenCode (`instructions`).
