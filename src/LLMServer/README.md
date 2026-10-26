# LocalLLM Control Center — Test build 0.1

Localhost dashboard to manage existing llama.cpp CUDA GGUF inference on Ubuntu 24.04 and configure OpenCode Desktop.

## Quick start

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip unzip
unzip LocalLLM-Control-Center.zip
cd LocalLLM-Control-Center
./run.sh
```

Open **http://127.0.0.1:7860** in a browser. The launcher creates `.venv` and installs FastAPI and Uvicorn on its first run. Internet is required for first installation and Hugging Face browsing/downloads. Ctrl+C stops dashboard; stop managed llama-server first.

## Expected locations

- CUDA server binary: `~/local-ai/src/llama.cpp/build-cuda/bin/llama-server`
- GGUF inventory: `~/local-ai/models` (recursive)
- OpenCode Desktop global config: `~/.config/opencode/opencode.json` (created by dashboard)
- Dashboard logs: `~/.local/share/localllm-control-center/llama-server.log`

If your locations differ, set environment variables *before* starting:

```bash
export LLAMA_SERVER_BIN="$HOME/llmCode/src/llama.cpp/build-cuda/bin/llama-server"
export LLM_MODEL_DIR="$HOME/local-ai/models"
./run.sh
```

You can inspect your actual binary via `find "$HOME" -type f -name llama-server 2>/dev/null` and model locations via `find "$HOME" -type f -iname '*.gguf' 2>/dev/null`. Only use directories you trust.

## How to test

1. **Local models:** verify discovered GGUFs. `mmproj` vision projectors are excluded from the deploy action.
2. **Deploy:** select the 4B Q4_K_M GGUF; start with GPU layers 4, context 8192, CPU threads 4. Watch the server status/logs; if VRAM OOM, reduce layers to 0.
3. **OpenCode:** select the matching alias and configuration schema; create the global config; restart OpenCode Desktop; choose the local model.
4. **Hugging Face:** search `Qwen3.5`, expand repository, choose a single GGUF. Download progress displays on the discovery tab. Files are stored under `<model dir>/<publisher>__<repo>/`.
5. **Stop:** only server processes launched by this dashboard can be stopped here. It does not terminate unrelated external services.

## Limits / safety notes

- This is a functional **test build**, not a production daemon or sandbox.
- All services bind to localhost; do not expose port 7860 through proxies/tunnels. No authentication is included.
- Downloads use the default branch. Gated/private repos and split GGUF models are not yet supported. Failed partial downloads stay as `.part` files; download resume and checksum verification are **not implemented** in v0.1. Validate downloaded files before important use.
- The download progress `Content-Length` may be unavailable. A response stream is still counted.
- Hugging Face model matches are not automatically verified for llama.cpp compatibility, license, coding ability or fit on the GTX 1050.
- On Linux, `llama-server` is launched as your current user, with no filesystem or network sandbox.
- OpenCode writer preserves existing **strict JSON** configuration, creates a timestamped backup, and adds the custom provider. JSONC configuration with comments must be edited manually. It may not apply policy to all project-level overrides.
- Desktop versions may use different OpenCode config schemas: standard `provider` (singular) vs V2 preview `providers` (plural). Verify in the installed Desktop's documentation.
- This app only manages the server process it starts; after restarting the dashboard it won't assume ownership of existing processes.
- Backend is API based; do not run untrusted extensions, proxy it to public interfaces or elevate it with sudo.

## Architecture

Python FastAPI + static HTML/CSS/JavaScript; standard Python urllib for Hugging Face API and download streaming; `nvidia-smi` and `/proc/meminfo` for system stats. There is no Electron or Node.js runtime.

## Developer validation

```bash
python3 -m py_compile app.py
bash -n run.sh
```
