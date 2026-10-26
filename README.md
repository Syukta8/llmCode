# llmCode (Cross-Platform Local LLM Server & Control Center)

A unified local AI coding framework supporting **Linux (Ubuntu/Debian)** and **Windows (10/11)**.
Seamlessly connect local open-weight models (`llama.cpp`, `KoboldCpp`, `Ollama`) with **VS Code Continue**, **Aider**, and **OpenCode CLI**.

---

## 🚀 Quickstart

### 1. Requirements
- **Linux**: Ubuntu 22.04/24.04, Python 3.10+, NVIDIA Driver + CUDA.
- **Windows**: Windows 10/11, Python 3.10+, NVIDIA Driver.
  - To compile locally against your exact GPU on Windows:
    - Install **CMake** (`winget install Kitware.CMake`)
    - Install **Ninja** (`winget install Ninja-build.Ninja`)
    - Install **Visual Studio C++ Build Tools** (`winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"`)

### 2. Setup Environment & Binaries
Standardized cross-platform installer:

```bash
# Linux:
./setup.sh
# or launch dashboard directly:
./setup.sh run

# Windows:
setup.bat
# or launch dashboard directly:
setup.bat run

# Or direct Python CLI:
python llmcode.py setup
```

---

## 🖥️ Windows Remote Setup & GPU Compilation Guide

If setting up on a clean Windows machine (e.g. RTX 5090 / 4090 workstation), follow these steps in PowerShell (Run as Administrator):

### Step 1: Install Build Tools & Git via Winget
```powershell
# 1. Install Git, CMake, Ninja, and Python (if not present)
winget install --id Git.Git -e --source winget
winget install --id Kitware.CMake -e --source winget
winget install --id Ninja-build.Ninja -e --source winget

# 2. Install Visual Studio 2022 C++ Compiler (cl.exe and Windows SDK)
winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive"
```

### Step 2: Open "x64 Native Tools Command Prompt for VS 2022"
Ensure `cl.exe`, `nvcc.exe`, and `cmake.exe` are in the environment PATH.

### Step 3: Compile llama.cpp for your GPU Architecture
```cmd
cd C:\Users\PC\llmCode
if not exist src\llama.cpp git clone https://github.com/ggml-org/llama.cpp.git src\llama.cpp
cmake -S src\llama.cpp -B src\llama.cpp\build-cuda -G "Ninja" -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON
cmake --build src\llama.cpp\build-cuda --target llama-server -j 8
```
*(Copy or symlink `llama-server.exe` to `bin\windows\llama-server.exe` or let `llmcode.py` auto-detect `src\llama.cpp\build-cuda\bin\llama-server.exe`)*.

### Step 4: Run llmCode Setup & Start Dashboard
```cmd
cd C:\Users\PC\llmCode
setup.bat run
```
Access dashboard at `http://127.0.0.1:7860`.

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
