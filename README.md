# llmCode (Cross-Platform Local LLM Server & Control Center)

A unified local AI coding framework supporting **Linux (Ubuntu/Debian)** and **Windows (10/11)**.
Seamlessly connect local open-weight models (`llama.cpp`, `KoboldCpp`, `Ollama`) with **VS Code Continue**, **Aider**, and **OpenCode CLI**.

---

## 🚀 Quickstart

### 1. Requirements
- **Linux**: Ubuntu 22.04/24.04, Python 3.10+, NVIDIA Driver + CUDA.
- **Windows**: Windows 10/11, Python 3.10+, NVIDIA Driver.

### 2. Setup Environment & Binaries
One command sets up the Python virtual environment and local backend binaries:

```bash
# Linux or Windows:
python llmcode.py setup
```
*(On Windows, this automatically resolves or downloads pre-built CUDA `llama-server.exe` binaries into `bin/windows/`)*.

---

## 📦 Usage

### Launch Web Dashboard (Port 7860)
Manage models, launch backends (`llama.cpp`, `KoboldCpp`, `Ollama`), and trigger Aider/OpenCode directly from the browser:

```bash
python llmcode.py dash
```
Open browser at `http://127.0.0.1:7860`.

---

### Serve a Model via CLI (Port 8080)
```bash
# Serve Qwen 2.5 Coder 7B with 8 GPU layers offloaded:
python llmcode.py serve 7b --layers 8 --context 8192

# Or serve 14B model:
python llmcode.py serve 14b --layers 10 --context 16384
```

---

### Download Models
```bash
# Download curated presets (4b, 7b, 14b):
python llmcode.py download 7b

# Or custom Hugging Face GGUF:
python llmcode.py download custom --repo "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF" --filename "qwen2.5-coder-7b-instruct-q4_k_m.gguf"
```

---

### Connect with VS Code Continue
In `~/.continue/config.yaml`:

```yaml
models:
  - name: Local Qwen 2.5 Coder
    provider: openai
    model: qwen2.5-coder-7b-instruct-q4_k_m
    apiBase: http://127.0.0.1:8080/v1
    apiKey: EMPTY
    roles:
      - chat
      - edit
      - apply
    capabilities:
      - tool_use
    defaultCompletionOptions:
      contextLength: 32768
```

---

### Launch Aider in Interactive Terminal
```bash
python llmcode.py aider --dir /path/to/project
```

---

## 🛠️ Architecture

```
llmCode/
├── llmcode.py               # Unified cross-platform CLI runner
├── src/
│   ├── common/              # Cross-platform abstractions
│   │   ├── paths.py         # Path resolution (Linux XDG vs Windows LocalAppData)
│   │   └── process.py       # Cross-platform terminal & process trees
│   ├── build/               # Binary setup & Windows CUDA prebuilt downloader
│   │   └── setup_binaries.py
│   └── LLMServer/           # Web Dashboard (FastAPI + uvicorn)
│       ├── app.py
│       └── static/index.html
├── bin/                     # Platform binaries (Windows/Linux)
└── models/                  # Local GGUF models
```
