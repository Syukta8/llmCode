# -*- coding: utf-8 -*-
"""
Cross-platform filesystem path resolver and configuration database for llmCode.
Uniformly resolves storage, configuration (`config/config.json`), and binary
locations across Windows, Linux, and macOS, with automatic path healing when
the repository directory moves or new binaries are compiled.
"""
import json
import os
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# Repository root directory
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Core portable directories inside repo root
CONFIG_DIR = REPO_ROOT / 'config'
LOG_DIR = REPO_ROOT / 'logs'
SRC_DIR = REPO_ROOT / 'src'
BIN_DIR = REPO_ROOT / 'bin'
MODEL_DIR = Path(os.environ.get('LLM_MODEL_DIR', str(REPO_ROOT / 'models'))).expanduser().resolve()

for _d in (CONFIG_DIR, LOG_DIR, MODEL_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Backward compatibility alias for modules referencing STORE_DIR
STORE_DIR = CONFIG_DIR

CONFIG_FILE = CONFIG_DIR / 'config.json'
PID_FILE = CONFIG_DIR / 'llmserver.pid'
LOG_FILE = LOG_DIR / 'llama-server.log'
DAEMON_LOG_FILE = LOG_DIR / 'llmserver-daemon.log'

# Legacy file pointers (kept only for migration if existing)
SETTINGS_FILE = CONFIG_FILE
DOWNLOADS_FILE = CONFIG_FILE

_config_lock = threading.RLock()


def get_platform_name() -> str:
    """Returns normalized platform identifier: 'windows', 'macos', or 'linux'."""
    if os.name == 'nt' or sys.platform.startswith('win'):
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    return 'linux'


def get_executable_name(name: str) -> str:
    """Appends .exe if running on Windows."""
    if os.name == 'nt' and not name.lower().endswith('.exe'):
        return f"{name}.exe"
    return name


def _discover_llama_server() -> Path:
    """Discovers llama-server binary across src/ build directories and bin/."""
    env_bin = os.environ.get('LLAMA_SERVER_BIN')
    if env_bin:
        p = Path(env_bin).expanduser().resolve()
        if p.is_file():
            return p

    exe = get_executable_name('llama-server')
    candidates = [
        SRC_DIR / 'llama.cpp' / 'build-cuda' / 'bin' / exe,
        SRC_DIR / 'llama.cpp' / 'build-cuda' / 'bin' / 'Release' / exe,
        SRC_DIR / 'llama.cpp' / 'build-metal' / 'bin' / exe,
        SRC_DIR / 'llama.cpp' / 'build' / 'bin' / exe,
        SRC_DIR / 'llama.cpp' / 'build' / 'bin' / 'Release' / exe,
        BIN_DIR / exe,
        BIN_DIR / 'windows' / exe,
        BIN_DIR / 'macos' / exe,
        BIN_DIR / 'linux' / exe,
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return candidates[0]


def _discover_koboldcpp() -> Path:
    """Discovers koboldcpp script or compiled binary across src/ and bin/."""
    env_bin = os.environ.get('KOBOLDCPP_BIN')
    if env_bin:
        p = Path(env_bin).expanduser().resolve()
        if p.is_file():
            return p

    exe = get_executable_name('koboldcpp')
    candidates = [
        SRC_DIR / 'koboldcpp' / 'koboldcpp.py',
        BIN_DIR / exe,
        BIN_DIR / 'windows' / exe,
        BIN_DIR / 'macos' / exe,
        BIN_DIR / 'linux' / exe,
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return candidates[0]


def _discover_ollama() -> Path:
    """Discovers ollama binary across src/ollama, bin/, and system PATH."""
    env_bin = os.environ.get('OLLAMA_BIN')
    if env_bin:
        p = Path(env_bin).expanduser().resolve()
        if p.is_file():
            return p

    exe = get_executable_name('ollama')
    candidates = [
        SRC_DIR / 'ollama' / exe,
        SRC_DIR / 'ollama' / 'build' / exe,
        BIN_DIR / 'ollama_dist' / 'bin' / exe,
        BIN_DIR / exe,
        BIN_DIR / 'windows' / exe,
        BIN_DIR / 'macos' / exe,
        BIN_DIR / 'linux' / exe,
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    found = shutil.which('ollama')
    if found:
        return Path(found).resolve()
    return candidates[0]


def _discover_aider() -> Optional[str]:
    """Discovers aider CLI executable across venv, user local bin, and PATH."""
    exe = get_executable_name('aider')
    venv_dir = SRC_DIR / 'LLMServer' / '.venv'
    candidates = [
        venv_dir / ('Scripts' if os.name == 'nt' else 'bin') / exe,
        Path.home() / '.local' / 'bin' / exe,
        Path.home() / 'AppData' / 'Roaming' / 'Python' / 'Scripts' / exe,
    ]
    for c in candidates:
        if c.is_file():
            return str(c.resolve())
    found = shutil.which('aider')
    return str(Path(found).resolve()) if found else None


def _discover_opencode() -> Optional[str]:
    """Discovers opencode CLI executable across ~/.opencode/bin and PATH."""
    exe = get_executable_name('opencode')
    candidates = [
        Path.home() / '.opencode' / 'bin' / exe,
        Path.home() / '.local' / 'bin' / exe,
    ]
    for c in candidates:
        if c.is_file():
            return str(c.resolve())
    found = shutil.which('opencode') or shutil.which('opencode2')
    return str(Path(found).resolve()) if found else None


def _default_config() -> Dict[str, Any]:
    """Returns default schema for config/config.json."""
    llama_bin = _discover_llama_server()
    kobold_bin = _discover_koboldcpp()
    ollama_bin = _discover_ollama()
    aider_bin = _discover_aider()
    opencode_bin = _discover_opencode()
    return {
        'version': 1,
        'platform': get_platform_name(),
        'updated_at': datetime.now().isoformat(timespec='seconds'),
        'paths': {
            'repo_root': str(REPO_ROOT),
            'config_dir': str(CONFIG_DIR),
            'model_dir': str(MODEL_DIR),
            'log_dir': str(LOG_DIR),
            'src_dir': str(SRC_DIR),
            'bin_dir': str(BIN_DIR),
        },
        'binaries': {
            'llama_cpp': str(llama_bin),
            'llama_cpp_exists': llama_bin.is_file(),
            'koboldcpp': str(kobold_bin),
            'koboldcpp_exists': kobold_bin.is_file(),
            'ollama': str(ollama_bin),
            'ollama_exists': ollama_bin.is_file(),
        },
        'agents': {
            'aider': aider_bin,
            'aider_installed': bool(aider_bin),
            'opencode': opencode_bin,
            'opencode_installed': bool(opencode_bin),
        },
        'settings': {
            'dash_port': int(os.environ.get('LLAMA_PORT_DASH', '7860')),
            'server_port': int(os.environ.get('LLAMA_PORT', '8080')),
            'last_model_path': '',
            'last_backend': 'llama.cpp',
            'last_alias': 'local-qwen',
            'last_layers': 8,
            'last_context': 8192,
            'last_threads': 4,
            'last_batch': 2048,
            'last_ubatch': 256,
            'last_flash_attn': 'on',
            'last_kv_cache': 'q8_0',
            'last_temp': 0.2,
            'last_top_p': 0.95,
            'last_top_k': 40,
            'last_min_p': 0.05,
            'last_repeat_penalty': 1.05,
            'last_main_gpu': 0,
            'last_split_mode': 'layer',
            'last_custom_flags': '',
            'last_project_dir': str(REPO_ROOT),
            'last_edit_format': 'diff',
        },
        'downloads': {},
    }


def load_config() -> Dict[str, Any]:
    """Loads config/config.json, initializing it with defaults if missing or corrupt."""
    with _config_lock:
        if CONFIG_FILE.is_file():
            try:
                raw = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                if isinstance(raw, dict):
                    base = _default_config()
                    for section in ('paths', 'binaries', 'agents', 'settings', 'downloads'):
                        if isinstance(raw.get(section), dict):
                            base[section].update(raw[section])
                    return base
            except Exception:
                pass
        cfg = _default_config()
        save_config(cfg)
        return cfg


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Writes configuration dictionary atomically to config/config.json."""
    with _config_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg['updated_at'] = datetime.now().isoformat(timespec='seconds')
        cfg['platform'] = get_platform_name()
        tmp_file = CONFIG_FILE.with_suffix('.json.tmp')
        tmp_file.write_text(json.dumps(cfg, indent=2) + '\n', encoding='utf-8')
        tmp_file.replace(CONFIG_FILE)
        return cfg


def sync_config() -> Dict[str, Any]:
    """
    Verifies all paths stored in config/config.json against the current filesystem.
    Automatically updates repo_root, model_dir, backend binaries, and CLI agent paths
    if the repository moved or new binaries were built/installed.
    """
    with _config_lock:
        cfg = load_config()
        old_root = cfg.get('paths', {}).get('repo_root', '')

        # Always enforce current canonical directory paths
        cfg['paths'] = {
            'repo_root': str(REPO_ROOT),
            'config_dir': str(CONFIG_DIR),
            'model_dir': str(MODEL_DIR),
            'log_dir': str(LOG_DIR),
            'src_dir': str(SRC_DIR),
            'bin_dir': str(BIN_DIR),
        }

        # Re-discover backend binaries if missing or moved
        def _resolve_or_discover(stored_path: str, discover_fn) -> Path:
            if stored_path and old_root == str(REPO_ROOT):
                p = Path(stored_path)
                if p.is_file():
                    return p.resolve()
            return discover_fn()

        llama_bin = _resolve_or_discover(cfg['binaries'].get('llama_cpp', ''), _discover_llama_server)
        kobold_bin = _resolve_or_discover(cfg['binaries'].get('koboldcpp', ''), _discover_koboldcpp)
        ollama_bin = _resolve_or_discover(cfg['binaries'].get('ollama', ''), _discover_ollama)

        cfg['binaries'] = {
            'llama_cpp': str(llama_bin),
            'llama_cpp_exists': llama_bin.is_file(),
            'koboldcpp': str(kobold_bin),
            'koboldcpp_exists': kobold_bin.is_file(),
            'ollama': str(ollama_bin),
            'ollama_exists': ollama_bin.is_file(),
        }

        # Re-discover CLI agents
        aider_bin = _discover_aider()
        opencode_bin = _discover_opencode()
        cfg['agents'] = {
            'aider': aider_bin,
            'aider_installed': bool(aider_bin),
            'opencode': opencode_bin,
            'opencode_installed': bool(opencode_bin),
        }

        # Heal last_model_path if repo moved or file missing
        last_model = cfg['settings'].get('last_model_path', '')
        if last_model:
            lm_path = Path(last_model)
            if not lm_path.is_file() and old_root and last_model.startswith(old_root):
                rel = last_model[len(old_root):].lstrip('/\\')
                candidate = REPO_ROOT / rel
                if candidate.is_file():
                    cfg['settings']['last_model_path'] = str(candidate.resolve())
                else:
                    cfg['settings']['last_model_path'] = ''
            elif not lm_path.is_file():
                cfg['settings']['last_model_path'] = ''

        # Heal last_project_dir if missing
        last_proj = cfg['settings'].get('last_project_dir', '')
        if not last_proj or not Path(last_proj).is_dir():
            cfg['settings']['last_project_dir'] = str(REPO_ROOT)

        return save_config(cfg)


def get_llama_server_binary() -> Path:
    """Resolves llama-server executable path via config database."""
    cfg = load_config()
    stored = cfg.get('binaries', {}).get('llama_cpp', '')
    if stored and Path(stored).is_file():
        return Path(stored).resolve()
    return _discover_llama_server()


def get_koboldcpp_binary() -> Path:
    """Resolves koboldcpp script or executable via config database."""
    cfg = load_config()
    stored = cfg.get('binaries', {}).get('koboldcpp', '')
    if stored and Path(stored).is_file():
        return Path(stored).resolve()
    return _discover_koboldcpp()


def get_ollama_binary() -> Path:
    """Resolves ollama executable via config database."""
    cfg = load_config()
    stored = cfg.get('binaries', {}).get('ollama', '')
    if stored and Path(stored).is_file():
        return Path(stored).resolve()
    return _discover_ollama()
