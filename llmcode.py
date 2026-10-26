#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llmCode - Unified Cross-Platform Local LLM CLI Runner
Provides a single interface for setup, downloading, serving, and dashboard
management across both Linux and Windows.
"""
import os
import sys
import shutil
import argparse
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.paths import (
    MODEL_DIR,
    STORE_DIR,
    LOG_FILE,
    get_llama_server_binary,
    get_koboldcpp_binary,
    get_ollama_binary,
)
from src.common.process import detect_terminal, spawn_in_terminal, kill_process_tree

def get_python_exe() -> str:
    """Returns path to virtualenv python if available, else sys.executable."""
    venv_dir = REPO_ROOT / 'src' / 'LLMServer' / '.venv'
    if os.name == 'nt':
        vpy = venv_dir / 'Scripts' / 'python.exe'
    else:
        vpy = venv_dir / 'bin' / 'python3'
    if vpy.is_file():
        return str(vpy)
    return sys.executable

def cmd_setup(args):
    """Setup dependencies, virtual environment, and backend binaries."""
    print(f"=== llmCode Setup ({sys.platform}) ===")
    server_dir = REPO_ROOT / 'src' / 'LLMServer'
    venv_dir = server_dir / '.venv'
    
    if not venv_dir.is_dir():
        print(f"Creating Python virtual environment in {venv_dir}...")
        subprocess.run([sys.executable, '-m', 'venv', str(venv_dir)], check=True)
    
    pip_exe = venv_dir / ('Scripts/pip.exe' if os.name == 'nt' else 'bin/pip')
    print("Installing requirements...")
    subprocess.run([str(pip_exe), 'install', '-q', '-r', str(server_dir / 'requirements.txt')], check=True)
    
    # Download/Compile binaries if missing
    llama_bin = get_llama_server_binary()
    if not llama_bin.is_file():
        print("llama-server binary not found. Running binary setup...")
        from src.build.setup_binaries import main as run_setup
        run_setup()
    else:
        print(f"Found llama-server at: {llama_bin}")
        
    print("Setup completed successfully.")

def cmd_download(args):
    """Download recommended models or Hugging Face GGUF files."""
    MODELS = {
        '4b': ('Qwen/Qwen2.5-Coder-7B-Instruct-GGUF', 'qwen2.5-coder-7b-instruct-q4_k_m.gguf'),
        '7b': ('Qwen/Qwen2.5-Coder-7B-Instruct-GGUF', 'qwen2.5-coder-7b-instruct-q4_k_m.gguf'),
        '14b': ('Qwen/Qwen2.5-Coder-14B-Instruct-GGUF', 'qwen2.5-coder-14b-instruct-q4_0.gguf'),
    }
    
    repo, filename = MODELS.get(args.model.lower(), (args.repo, args.filename))
    if not repo or not filename:
        print(f"Unknown preset '{args.model}'. Choose from: 4b, 7b, 14b, or pass --repo and --filename.")
        return 1

    dest = MODEL_DIR / repo.replace('/', '__') / filename
    if dest.is_file() and dest.stat().st_size > 100_000_000:
        print(f"Model already exists at: {dest}")
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
    print(f"Downloading {repo} -> {dest} ...")
    
    try:
        from huggingface_hub import hf_hub_download
        hf_hub_download(repo_id=repo, filename=filename, local_dir=str(dest.parent))
    except Exception:
        import urllib.request
        req = urllib.request.Request(url, headers={'User-Agent': 'llmCode/1.0'})
        with urllib.request.urlopen(req) as resp, open(dest, 'wb') as f:
            shutil.copyfileobj(resp, f)
            
    print(f"Download complete: {dest}")

def cmd_serve(args):
    """Serve a local GGUF model via llama-server."""
    llama_bin = get_llama_server_binary()
    if not llama_bin.is_file():
        print(f"Error: llama-server binary not found at {llama_bin}. Run 'python llmcode.py setup' first.")
        return 1
        
    # Find matching model
    model_file = None
    if args.model:
        for p in MODEL_DIR.rglob('*.gguf'):
            if args.model.lower() in p.name.lower():
                model_file = p
                break
    if not model_file:
        ggufs = list(MODEL_DIR.rglob('*.gguf'))
        if not ggufs:
            print("No GGUF models found in models/. Run 'python llmcode.py download 7b' first.")
            return 1
        model_file = ggufs[0]
        
    print(f"Serving model: {model_file.name}")
    print(f"Offloading {args.layers} GPU layers | Context: {args.context} | Port: {args.port}")
    
    cmd = [
        str(llama_bin),
        '--model', str(model_file),
        '--alias', args.alias or model_file.stem,
        '--host', '127.0.0.1',
        '--port', str(args.port),
        '--ctx-size', str(args.context),
        '--n-gpu-layers', str(args.layers),
        '--threads', str(args.threads),
        '--parallel', '1',
        '--jinja',
        '-fa', 'on',
        '-ctk', 'q8_0',
        '-ctv', 'q8_0',
    ]
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        print("\nServer stopped.")

def cmd_dash(args):
    """Launch the Web Dashboard (FastAPI server on port 7860)."""
    server_dir = REPO_ROOT / 'src' / 'LLMServer'
    py_exe = get_python_exe()
    
    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{args.port}")
    
    cmd = [
        py_exe, '-m', 'uvicorn', 'app:app',
        '--app-dir', str(server_dir),
        '--host', '127.0.0.1',
        '--port', str(args.port)
    ]
    print(f"Starting dashboard at http://127.0.0.1:{args.port} ...")
    subprocess.run(cmd)

def cmd_aider(args):
    """Spawn Aider connected to the running local server in an interactive terminal."""
    target_dir = Path(args.dir).expanduser().resolve()
    aider_cmd = f"aider --openai-api-base http://127.0.0.1:{args.port}/v1 --openai-api-key none --stream"
    print(f"Launching Aider in {target_dir}...")
    spawn_in_terminal(aider_cmd, cwd=target_dir, title="llmCode Aider")

def main():
    parser = argparse.ArgumentParser(description="llmCode - Cross-Platform Local LLM Runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # setup
    p_setup = subparsers.add_parser("setup", help="Set up environment and binaries")
    p_setup.set_defaults(func=cmd_setup)

    # download
    p_dl = subparsers.add_parser("download", help="Download GGUF model")
    p_dl.add_argument("model", nargs="?", default="7b", help="Preset: 4b, 7b, 14b")
    p_dl.add_argument("--repo", default="", help="Hugging Face repo ID")
    p_dl.add_argument("--filename", default="", help="GGUF filename")
    p_dl.set_defaults(func=cmd_download)

    # serve
    p_serve = subparsers.add_parser("serve", help="Run llama-server")
    p_serve.add_argument("model", nargs="?", default="", help="Model name or substring")
    p_serve.add_argument("--layers", type=int, default=8, help="GPU layers to offload")
    p_serve.add_argument("--context", type=int, default=8192, help="Context size")
    p_serve.add_argument("--threads", type=int, default=4, help="CPU threads")
    p_serve.add_argument("--port", type=int, default=8080, help="Port to listen on")
    p_serve.add_argument("--alias", default="", help="Model alias")
    p_serve.set_defaults(func=cmd_serve)

    # dash
    p_dash = subparsers.add_parser("dash", help="Start Web Dashboard")
    p_dash.add_argument("--port", type=int, default=7860, help="Dashboard port")
    p_dash.set_defaults(func=cmd_dash)

    # aider
    p_aider = subparsers.add_parser("aider", help="Launch Aider CLI")
    p_aider.add_argument("--dir", default=".", help="Project directory")
    p_aider.add_argument("--port", type=int, default=8080, help="Server port")
    p_aider.set_defaults(func=cmd_aider)

    args = parser.parse_args()
    return args.func(args)

if __name__ == '__main__':
    sys.exit(main() or 0)
