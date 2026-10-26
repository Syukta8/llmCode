# llmCode

Unified local LLM server and coding station supporting **Windows**, **Linux**, and **macOS**.
Scans your hardware & packages into `config/hardware.json`, auto-tunes `llama.cpp`, `KoboldCpp`, and `Ollama` compilation & settings, installs **Aider** and **OpenCode** inside the `llm-dash` virtual environment, and configures **VS Code Continue** with standard aliases (`llamacpp`, `koboldcpp`, `ollama`).

---

## 1. Windows Setup & Usage

```cmd
:: 1. Scan hardware, compile/download backends, install Aider & OpenCode in .venv, configure Continue
setup.bat

:: 2. Launch llm-dash in background and open browser (http://127.0.0.1:7860)
launch.bat

:: 3. Stop llm-dash and active inference backends
stop.bat

:: 4. Full uninstall (removes .venv, compiled engines, binaries, models, configs, logs)
setup.bat --uninstall
```

---

## 2. Linux Setup & Usage

```bash
# 1. Scan hardware, compile/download backends, install Aider & OpenCode in .venv, configure Continue
./setup.sh

# 2. Launch llm-dash in background and open browser (http://127.0.0.1:7860)
./launch.sh

# 3. Stop llm-dash and active inference backends
./stop.sh

# 4. Full uninstall (removes .venv, compiled engines, binaries, models, configs, logs)
./setup.sh --uninstall
```

---

## 3. macOS Setup & Usage

```bash
# 1. Scan hardware (Apple Metal), compile/download backends, install Aider & OpenCode in .venv, configure Continue
./setup.sh

# 2. Launch llm-dash in background and open browser (http://127.0.0.1:7860)
./launch.sh

# 3. Stop llm-dash and active inference backends
./stop.sh

# 4. Full uninstall (removes .venv, compiled engines, binaries, models, configs, logs)
./setup.sh --uninstall
```

---

## Standard Backend Aliases (`http://127.0.0.1:8080/v1`)

All integrations (**VS Code Continue**, **Aider**, **OpenCode**) connect using a fixed alias per backend engine so you never need to rewrite config files when switching GGUF models:
- `llama.cpp` -> **`llamacpp`**
- `KoboldCpp` -> **`koboldcpp`**
- `Ollama` -> **`ollama`**

---

## Folder Structure

```text
llmCode/
├── setup.bat / setup.sh     # Hardware scanner, engine installer, .venv agent setup & --uninstall
├── launch.bat / launch.sh   # Auto-syncs config/config.json and starts llm-dash daemon
├── stop.bat / stop.sh       # Stops llm-dash and all active backend servers
├── llmcode.py               # Cross-platform CLI runner
├── config/
│   ├── hardware.json        # Auto-scanned CPU/RAM/GPU/VRAM, package paths & build/runtime tuning
│   ├── config.json          # Auto-healed database (paths, binaries, settings, downloads)
│   ├── rag_index.json       # Local BM25/TF-IDF workspace chunk index (when RAG is enabled)
│   └── rag_context.md       # Auto-generated workspace RAG context injected into Aider & OpenCode
├── src/
│   ├── llama.cpp/           # llama.cpp source & build
│   ├── koboldcpp/           # KoboldCpp source & build
│   ├── ollama/              # Ollama source & build
│   ├── build/               # Hardware scanner, prerequisite checker, compiler & uninstaller
│   ├── common/              # Cross-platform paths, process management & Workspace RAG engine
│   └── LLMServer/           # FastAPI Web Dashboard (llm-dash) + .venv (Aider & OpenCode)
├── bin/                     # Prebuilt binaries & desktop assets
├── models/                  # Local GGUF models
└── logs/                    # Daemon & inference engine logs
```
