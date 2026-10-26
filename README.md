# llmCode

Unified local LLM server and coding station supporting **Windows**, **Linux**, and **macOS**.
Manage `llama.cpp`, `KoboldCpp`, and `Ollama` backends and launch **Aider** or **OpenCode** CLI from a single web dashboard (`llm-dash`).

---

## 1. Windows Setup & Usage

```cmd
:: 1. Install environment, compile/download backends & CLI agents
setup.bat

:: 2. Launch llm-dash in background and open browser (http://127.0.0.1:7860)
launch.bat

:: 3. Stop llm-dash and active inference backends
stop.bat
```

---

## 2. Linux Setup & Usage

```bash
# 1. Install environment, compile/download backends & CLI agents
./setup.sh

# 2. Launch llm-dash in background and open browser (http://127.0.0.1:7860)
./launch.sh

# 3. Stop llm-dash and active inference backends
./stop.sh
```

---

## 3. macOS Setup & Usage

```bash
# 1. Install environment, compile/download backends (Apple Metal) & CLI agents
./setup.sh

# 2. Launch llm-dash in background and open browser (http://127.0.0.1:7860)
./launch.sh

# 3. Stop llm-dash and active inference backends
./stop.sh
```

---

## Folder Structure

```text
llmCode/
├── setup.bat / setup.sh     # Interactive installer (prerequisites, engines, CLI agents)
├── launch.bat / launch.sh   # Auto-syncs config/config.json and starts llm-dash daemon
├── stop.bat / stop.sh       # Stops llm-dash and all active backend servers
├── llmcode.py               # Cross-platform CLI runner
├── config/
│   └── config.json          # Auto-healed database (paths, binaries, settings, downloads)
├── src/
│   ├── llama.cpp/           # llama.cpp source & build
│   ├── koboldcpp/           # KoboldCpp source & build
│   ├── ollama/              # Ollama source & build
│   ├── build/               # Prerequisite checker & engine compiler/downloader
│   ├── common/              # Cross-platform path & process management
│   └── LLMServer/           # FastAPI Web Dashboard (llm-dash)
├── bin/                     # Prebuilt binaries & desktop assets
├── models/                  # Local GGUF models
└── logs/                    # Daemon & inference engine logs
```
