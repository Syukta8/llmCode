# -*- coding: utf-8 -*-
"""
Cross-platform filesystem path resolver, hardware profile loader (`config/hardware.json`),
and configuration database (`config/config.json`) for llmCode.
Uniformly resolves storage, configuration, standard aliases (`llamacpp`, `koboldcpp`, `ollama`),
and binary locations across Windows, Linux, and macOS, with automatic path healing and
hardware-driven default tuning.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
HARDWARE_FILE = CONFIG_DIR / 'hardware.json'
PID_FILE = CONFIG_DIR / 'llmserver.pid'
LOG_FILE = LOG_DIR / 'llama-server.log'
DAEMON_LOG_FILE = LOG_DIR / 'llmserver-daemon.log'
API_KEY_FILE = CONFIG_DIR / 'api_key'
AGENT_ENV_FILE = CONFIG_DIR / 'agent.env'
DEPLOY_STATE_FILE = CONFIG_DIR / 'deploy_state.json'

# Legacy file pointers
SETTINGS_FILE = CONFIG_FILE
DOWNLOADS_FILE = CONFIG_FILE

STANDARD_ALIASES = {
    'llama.cpp': 'llamacpp',
    'koboldcpp': 'koboldcpp',
    'ollama': 'ollama',
}

_config_lock = threading.RLock()


def no_window_kwargs() -> Dict[str, Any]:
    """Returns Windows CREATE_NO_WINDOW creationflags for silent background subprocesses."""
    if os.name == 'nt':
        return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}
    return {}


def write_secret(path: Path, content: str) -> None:
    """Writes sensitive credentials atomically with restricted user-only permissions (0o600 or Windows ACL)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f'.tmp.{os.getpid()}')
    try:
        # On POSIX, set umask or create with 0o600
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        mode = 0o600
        fd = os.open(str(tmp_path), flags, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)

        if os.name == 'nt':
            try:
                username = os.environ.get('USERNAME', '')
                if username:
                    subprocess.run(
                        ['icacls', str(tmp_path), '/inheritance:r', '/grant:r', f'{username}:F'],
                        capture_output=True,
                        check=False,
                        **no_window_kwargs(),
                    )
            except Exception:
                pass
        else:
            try:
                os.chmod(tmp_path, 0o600)
            except Exception:
                pass

        os.replace(tmp_path, path)
        if os.name != 'nt':
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def get_standard_alias(backend: str) -> str:
    """Returns the standard API model alias ('llamacpp', 'koboldcpp', or 'ollama') for a backend."""
    return STANDARD_ALIASES.get(backend, 'llamacpp')


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


def build_binary_env(bin_path: Path, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Constructs an environment dictionary with the binary's parent directory prepended
    to PATH (Windows DLLs), LD_LIBRARY_PATH (Linux .so), and DYLD_LIBRARY_PATH (macOS .dylib)
    so prebuilt and source-built shared libraries resolve deterministically on all platforms.
    """
    env = dict(base_env if base_env is not None else os.environ)
    try:
        bin_parent = str(Path(bin_path).resolve().parent)
    except Exception:
        bin_parent = str(Path(bin_path).parent)
    env['PATH'] = bin_parent + os.pathsep + env.get('PATH', '')
    plat = get_platform_name()
    if plat == 'linux':
        cur_ld = env.get('LD_LIBRARY_PATH', '')
        env['LD_LIBRARY_PATH'] = f"{bin_parent}:{cur_ld}" if cur_ld else bin_parent
    elif plat == 'macos':
        cur_dyld = env.get('DYLD_LIBRARY_PATH', '')
        env['DYLD_LIBRARY_PATH'] = f"{bin_parent}:{cur_dyld}" if cur_dyld else bin_parent
        cur_fb = env.get('DYLD_FALLBACK_LIBRARY_PATH', '')
        env['DYLD_FALLBACK_LIBRARY_PATH'] = f"{bin_parent}:{cur_fb}" if cur_fb else bin_parent
    return env


def load_hardware() -> Dict[str, Any]:
    """Loads config/hardware.json if present, else returns an empty dict."""
    with _config_lock:
        if HARDWARE_FILE.is_file():
            try:
                data = json.loads(HARDWARE_FILE.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}


def save_hardware(data: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically writes hardware & package scan results to config/hardware.json."""
    with _config_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data['updated_at'] = datetime.now().isoformat(timespec='seconds')
        data['platform'] = get_platform_name()
        tmp_file = HARDWARE_FILE.with_suffix('.json.tmp')
        tmp_file.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
        tmp_file.replace(HARDWARE_FILE)
        return data


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
        BIN_DIR / 'windows' / exe,
        BIN_DIR / 'macos' / exe,
        BIN_DIR / 'linux' / exe,
        BIN_DIR / exe,
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    for plat_sub in ('windows', 'macos', 'linux'):
        sub_dir = BIN_DIR / plat_sub
        if sub_dir.is_dir():
            for p in sub_dir.rglob(exe):
                if p.is_file():
                    return p.resolve()
    return candidates[0]


def _discover_koboldcpp() -> Path:
    """Discovers koboldcpp script or compiled binary across src/ and bin/."""
    env_bin = os.environ.get('KOBOLDCPP_BIN')
    if env_bin:
        p = Path(env_bin).expanduser().resolve()
        if p.is_file():
            return p

    exe = get_executable_name('koboldcpp')
    plat = get_platform_name()
    src_script = SRC_DIR / 'koboldcpp' / 'koboldcpp.py'
    src_has_compiled_lib = False
    if src_script.is_file():
        for ext in ('*.so', '*.dll', '*.dylib'):
            if list((SRC_DIR / 'koboldcpp').glob(f'koboldcpp_*{ext[1:]}')):
                src_has_compiled_lib = True
                break

    candidates = []
    if src_has_compiled_lib:
        candidates.append(src_script)
    candidates.extend([
        BIN_DIR / plat / exe,
        BIN_DIR / exe,
        BIN_DIR / 'windows' / exe,
        BIN_DIR / 'macos' / exe,
        BIN_DIR / 'linux' / exe,
        src_script,
    ])
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return BIN_DIR / plat / exe


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


def parse_version_tuple(version_str: str) -> Tuple[int, int, int]:
    """Extracts (major, minor, patch) integers from a CLI --version output string."""
    import re
    m = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', version_str or '')
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _discover_opencode() -> Optional[str]:
    """Discovers OpenCode v2 (`@opencode/cli` >= 2.0.0) CLI executable prioritizing llm-dash venv, rejecting legacy v1 (< 2.0.0) binaries."""
    exe = get_executable_name('opencode')
    venv_dir = SRC_DIR / 'LLMServer' / '.venv'
    venv_bin_dir = venv_dir / ('Scripts' if os.name == 'nt' else 'bin')
    candidates = [
        venv_bin_dir / exe,
        venv_bin_dir / 'opencode2',
        venv_bin_dir / 'opencode.cmd',
        venv_bin_dir / 'opencode2.cmd',
        venv_dir / 'node_modules' / '@opencode' / 'cli' / 'bin' / 'opencode.exe',
        venv_dir / 'node_modules' / '@opencode' / 'cli' / 'bin' / exe,
        venv_dir / 'node_modules' / '.bin' / ('opencode.cmd' if os.name == 'nt' else 'opencode'),
        Path.home() / '.local' / 'bin' / exe,
    ]
    for c in candidates:
        if c.is_file():
            try:
                cmd_target = str(c.resolve())
                if os.name == 'nt' and cmd_target.lower().endswith(('.cmd', '.bat')):
                    res = subprocess.run(['cmd.exe', '/c', cmd_target, '--version'], capture_output=True, text=True, timeout=3, **no_window_kwargs())
                else:
                    res = subprocess.run([cmd_target, '--version'], capture_output=True, text=True, timeout=3, **no_window_kwargs())
                if res.returncode == 0:
                    ver_out = (res.stdout or '') + (res.stderr or '')
                    ver = parse_version_tuple(ver_out)
                    # Reject legacy v1 (0.x / 1.x opencode-ai) binaries; require v2 architecture (2.0.0+)
                    if ver >= (2, 0, 0):
                        return cmd_target
            except Exception:
                pass
    found = shutil.which('opencode2') or shutil.which('opencode')
    if found:
        try:
            cmd_target = str(Path(found).resolve())
            if os.name == 'nt' and cmd_target.lower().endswith(('.cmd', '.bat')):
                res = subprocess.run(['cmd.exe', '/c', cmd_target, '--version'], capture_output=True, text=True, timeout=3)
            else:
                res = subprocess.run([cmd_target, '--version'], capture_output=True, text=True, timeout=3)
            if res.returncode == 0:
                ver_out = (res.stdout or '') + (res.stderr or '')
                ver = parse_version_tuple(ver_out)
                if ver >= (2, 0, 0):
                    return cmd_target
        except Exception:
            pass
    return None


# Floors for npm-scoped harness CLIs; older builds lack the custom-endpoint flags llmCode relies on.
HARNESS_MIN_VERSIONS: Dict[str, Tuple[int, int, int]] = {
    'claude': (1, 0, 0),
    'codex': (0, 40, 0),
}


def _discover_npm_harness(name: str) -> Optional[str]:
    """Discovers an npm-installed harness CLI (`claude`, `codex`) prioritizing llm-dash .venv, enforcing HARNESS_MIN_VERSIONS."""
    venv_dir = SRC_DIR / 'LLMServer' / '.venv'
    venv_bin_dir = venv_dir / ('Scripts' if os.name == 'nt' else 'bin')
    shim = f'{name}.cmd' if os.name == 'nt' else name
    candidates = [venv_bin_dir / shim, venv_dir / 'node_modules' / '.bin' / shim]
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    floor = HARNESS_MIN_VERSIONS.get(name, (0, 0, 0))
    for c in candidates:
        if not c.is_file():
            continue
        try:
            target = str(c.resolve()) if c.suffix.lower() not in ('.cmd', '.bat') else str(c)
            argv = ['cmd.exe', '/c', target, '--version'] if os.name == 'nt' and target.lower().endswith(('.cmd', '.bat')) else [target, '--version']
            res = subprocess.run(argv, capture_output=True, text=True, timeout=10, **no_window_kwargs())
            if res.returncode == 0 and parse_version_tuple((res.stdout or '') + (res.stderr or '')) >= floor:
                return str(c)
        except Exception:
            pass
    return None


VALID_HARNESSES: Tuple[str, ...] = ('opencode', 'claude', 'codex')


def discover_agents() -> Dict[str, Any]:
    """Builds the `agents` config section for every supported harness (opencode, claude, codex)."""
    out: Dict[str, Any] = {}
    for name, fn in (('opencode', _discover_opencode), ('claude', lambda: _discover_npm_harness('claude')), ('codex', lambda: _discover_npm_harness('codex'))):
        b = fn()
        out[name] = b
        out[f'{name}_installed'] = bool(b)
    return out


def _default_config() -> Dict[str, Any]:
    """Returns default schema for config/config.json, tuned by config/hardware.json if present."""
    llama_bin = _discover_llama_server()
    kobold_bin = _discover_koboldcpp()
    ollama_bin = _discover_ollama()
    hw = load_hardware()
    rt = hw.get('runtime_defaults', {})

    return {
        'version': 1,
        'platform': get_platform_name(),
        'updated_at': datetime.now().isoformat(timespec='seconds'),
        'paths': {
            'repo_root': str(REPO_ROOT),
            'config_dir': str(CONFIG_DIR),
            'hardware_file': str(HARDWARE_FILE),
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
        'agents': discover_agents(),
        'selected_agents': None,
        'settings': {
            'dash_port': int(os.environ.get('LLAMA_PORT_DASH', '7860')),
            'server_port': int(os.environ.get('LLAMA_PORT', '8080')),
            'last_model_path': '',
            'last_backend': 'llama.cpp',
            'last_alias': 'llamacpp',
            'last_layers': int(rt.get('layers', 8)),
            'last_context': int(rt.get('context', 8192)),
            'last_parallel': int(rt.get('parallel', 1)),
            'last_threads': int(rt.get('threads', 4)),
            'last_threads_batch': int(rt.get('threads_batch', 6)),
            'last_batch': int(rt.get('batch', 2048)),
            'last_ubatch': int(rt.get('ubatch', 256)),
            'last_flash_attn': str(rt.get('flash_attn', 'on')),
            'last_kv_cache': str(rt.get('kv_cache', 'q8_0')),
            'last_context_extend': 'none',
            'last_rag_enabled': False,
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
                    for k, v in raw.items():
                        if k not in base:
                            base[k] = v
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


def sync_config(apply_hardware_defaults: bool = False) -> Dict[str, Any]:
    """
    Verifies all paths stored in config/config.json against the current filesystem.
    Automatically updates repo_root, model_dir, backend binaries, and CLI agent paths
    if the repository moved or new binaries were built/installed.
    If apply_hardware_defaults is True (or config.json is newly created), applies
    recommended layers/context/threads/batch from config/hardware.json.
    """
    with _config_lock:
        is_new = not CONFIG_FILE.is_file()
        cfg = load_config()
        old_root = cfg.get('paths', {}).get('repo_root', '')

        # Always enforce current canonical directory paths
        cfg['paths'] = {
            'repo_root': str(REPO_ROOT),
            'config_dir': str(CONFIG_DIR),
            'hardware_file': str(HARDWARE_FILE),
            'model_dir': str(MODEL_DIR),
            'log_dir': str(LOG_DIR),
            'src_dir': str(SRC_DIR),
            'bin_dir': str(BIN_DIR),
        }

        def _resolve_or_discover(stored_path: str, discover_fn) -> Path:
            disc = discover_fn()
            # If discovery found an active binary compiled from source in src/, prioritize it over prebuilt bin/
            if disc and disc.is_file() and 'src' in disc.parts:
                return disc
            if stored_path and old_root == str(REPO_ROOT):
                p = Path(stored_path)
                if p.is_file():
                    return p.resolve()
            return disc

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

        cfg['agents'] = discover_agents()

        # Apply hardware-tuned runtime defaults when requested or on first initialization
        hw = load_hardware()
        rt = hw.get('runtime_defaults', {})
        if (apply_hardware_defaults or is_new) and rt:
            cfg['settings']['last_layers'] = int(rt.get('layers', cfg['settings'].get('last_layers', 8)))
            cfg['settings']['last_context'] = int(rt.get('context', cfg['settings'].get('last_context', 8192)))
            cfg['settings']['last_parallel'] = int(rt.get('parallel', cfg['settings'].get('last_parallel', 1)))
            cfg['settings']['last_threads'] = int(rt.get('threads', cfg['settings'].get('last_threads', 4)))
            cfg['settings']['last_threads_batch'] = int(rt.get('threads_batch', cfg['settings'].get('last_threads_batch', 6)))
            cfg['settings']['last_batch'] = int(rt.get('batch', cfg['settings'].get('last_batch', 2048)))
            cfg['settings']['last_ubatch'] = int(rt.get('ubatch', cfg['settings'].get('last_ubatch', 256)))
            cfg['settings']['last_flash_attn'] = str(rt.get('flash_attn', cfg['settings'].get('last_flash_attn', 'on')))
            cfg['settings']['last_kv_cache'] = str(rt.get('kv_cache', cfg['settings'].get('last_kv_cache', 'q8_0')))

        # Normalize alias to standard alias ('llamacpp', 'koboldcpp', 'ollama') if legacy default 'local-qwen'
        backend = cfg['settings'].get('last_backend', 'llama.cpp')
        cur_alias = cfg['settings'].get('last_alias', '')
        if not cur_alias or cur_alias == 'local-qwen':
            cfg['settings']['last_alias'] = get_standard_alias(backend)

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

        last_proj = cfg['settings'].get('last_project_dir', '')
        if not last_proj or not Path(last_proj).is_dir():
            cfg['settings']['last_project_dir'] = str(REPO_ROOT)

        return save_config(cfg)


def get_selected_agents(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """Returns explicitly selected harnesses from config, or infers from installed agents for legacy configs."""
    data = cfg if cfg is not None else load_config()
    raw = data.get('selected_agents')
    if isinstance(raw, list):
        return [h for h in VALID_HARNESSES if h in raw]
    agents = data.get('agents', {})
    installed = [h for h in VALID_HARNESSES if agents.get(f'{h}_installed')]
    return installed or list(VALID_HARNESSES)


def save_selected_agents(selected: List[str]) -> Dict[str, Any]:
    """Persists the user's chosen CLI harness subset into config/config.json."""
    cfg = load_config()
    cfg['selected_agents'] = [h for h in VALID_HARNESSES if h in selected]
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
