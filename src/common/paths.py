# -*- coding: utf-8 -*-
"""
Cross-platform filesystem path resolver for llmCode.
Uniformly resolves storage, configuration, and binary locations across Linux and Windows.
"""
import os
import sys
from pathlib import Path

# Repository root directory
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

def get_store_dir() -> Path:
    """Returns application data and runtime state directory."""
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
        store = base / 'llmCode'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))
        store = base / 'llmCode'
    store.mkdir(parents=True, exist_ok=True)
    return store

STORE_DIR = get_store_dir()
MODEL_DIR = Path(os.environ.get('LLM_MODEL_DIR', str(REPO_ROOT / 'models'))).expanduser().resolve()
MODEL_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = STORE_DIR / 'state.json'
LOG_FILE = STORE_DIR / 'llama-server.log'
SETTINGS_FILE = STORE_DIR / 'settings.json'
DOWNLOADS_FILE = STORE_DIR / 'downloads.json'
PID_FILE = STORE_DIR / 'llmserver.pid'

def get_executable_name(name: str) -> str:
    """Appends .exe if running on Windows."""
    if os.name == 'nt' and not name.lower().endswith('.exe'):
        return f"{name}.exe"
    return name

def get_llama_server_binary() -> Path:
    """Resolves llama-server executable path across platforms."""
    env_bin = os.environ.get('LLAMA_SERVER_BIN')
    if env_bin:
        return Path(env_bin).expanduser().resolve()
    
    exe = get_executable_name('llama-server')
    candidates = [
        REPO_ROOT / 'src' / 'llama.cpp' / 'build-cuda' / 'bin' / exe,
        REPO_ROOT / 'src' / 'llama.cpp' / 'build' / 'bin' / exe,
        REPO_ROOT / 'src' / 'llama.cpp' / 'build' / 'bin' / 'Release' / exe,
        REPO_ROOT / 'bin' / exe,
        REPO_ROOT / 'bin' / 'windows' / exe,
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return candidates[0]

def get_koboldcpp_binary() -> Path:
    """Resolves koboldcpp script or executable."""
    exe = get_executable_name('koboldcpp')
    candidates = [
        REPO_ROOT / 'src' / 'koboldcpp' / 'koboldcpp.py',
        REPO_ROOT / 'bin' / exe,
        REPO_ROOT / 'bin' / 'windows' / exe,
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return candidates[0]

def get_ollama_binary() -> Path:
    """Resolves ollama executable across platforms."""
    exe = get_executable_name('ollama')
    candidates = [
        REPO_ROOT / 'bin' / 'ollama_dist' / 'bin' / exe,
        REPO_ROOT / 'bin' / exe,
        REPO_ROOT / 'bin' / 'windows' / exe,
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    import shutil
    found = shutil.which('ollama')
    if found:
        return Path(found).resolve()
    return candidates[1]
