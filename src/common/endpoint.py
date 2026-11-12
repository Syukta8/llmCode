"""
Resolves which local base URL a harness should use.

Why: llama.cpp's llama-server serves `/v1/messages` (Anthropic) and `/v1/responses` (OpenAI Responses)
natively, but koboldcpp/ollama and older llama.cpp builds only speak chat completions. We probe the
engine first and only pay for a LiteLLM translation proxy when the native route is missing.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.common.paths import CONFIG_DIR, LOG_DIR, REPO_ROOT

PROXY_PORT_OFFSET = 100
PROXY_CONFIG_FILE = CONFIG_DIR / 'litellm_proxy.yaml'
PROXY_LOG_FILE = LOG_DIR / 'litellm-proxy.log'

_lock = threading.Lock()
_proxy: Dict[str, Any] = {'proc': None, 'signature': None, 'port': None}


def probe_native(engine_url: str, style: str, alias: str, api_key: str, timeout: float = 60.0) -> bool:
    """Sends a 1-token request to the native route; any 2xx means the engine handles the protocol itself."""
    if style == 'anthropic':
        # Multi-turn probe: Claude Code sends mid-turn system messages (<system-reminder>, environment context).
        # Engines whose chat templates forbid mid-turn system messages (e.g. Qwen on llama-server) return 500,
        # which triggers the LiteLLM translation proxy that merges them into turn 0.
        path, body = '/v1/messages', {
            'model': alias,
            'max_tokens': 1,
            'system': 'ping',
            'messages': [
                {'role': 'user', 'content': 'ping'},
                {'role': 'system', 'content': 'ping-sys'},
            ],
        }
    elif style == 'responses':
        path, body = '/v1/responses', {'model': alias, 'max_output_tokens': 16, 'input': 'ping'}
    else:
        return True
    req = urllib.request.Request(
        engine_url.rstrip('/') + path,
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}', 'x-api-key': api_key,
                 'anthropic-version': '2023-06-01'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _proxy_yaml(engine_url: str, alias: str) -> str:
    """LiteLLM config exposing the alias plus a `*` catch-all so harness-internal model names still route locally."""
    entries = []
    for name in (alias, '*'):
        entries.append(
            f'  - model_name: {json.dumps(name)}\n'
            f'    litellm_params:\n'
            f'      model: {json.dumps("openai/" + alias)}\n'
            f'      api_base: {json.dumps(engine_url.rstrip("/") + "/v1")}\n'
            f'      api_key: os.environ/LLMCODE_ENGINE_KEY\n'
        )
    return (
        'model_list:\n' + ''.join(entries) +
        'litellm_settings:\n'
        '  drop_params: true\n'
        '  callbacks:\n'
        '    - src.common.litellm_hooks.proxy_effort_hook\n'
        'general_settings:\n'
        '  master_key: os.environ/LLMCODE_ENGINE_KEY\n'
        '  dangerously_permit_weak_or_unset_master_key: true\n'
    )


def _litellm_bin() -> Path:
    bin_dir = Path(sys.executable).parent
    return bin_dir / ('litellm.exe' if sys.platform == 'win32' else 'litellm')


def ensure_litellm_proxy(engine_url: str, engine_port: int, alias: str, api_key: str) -> str:
    """Starts (or reuses) a loopback LiteLLM proxy on engine_port+100; restarts it when alias/engine/key change."""
    port = engine_port + PROXY_PORT_OFFSET
    signature: Tuple[str, str, str, int] = (engine_url, alias, api_key, port)
    with _lock:
        p = _proxy['proc']
        if p is not None and p.poll() is None and _proxy['signature'] == signature:
            return f'http://127.0.0.1:{port}'
        _stop_locked()
        exe = _litellm_bin()
        if not exe.is_file():
            raise RuntimeError('LiteLLM proxy not installed in .venv; rerun setup (litellm[proxy]).')
        PROXY_CONFIG_FILE.write_text(_proxy_yaml(engine_url, alias), encoding='utf-8')
        PROXY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing_pypath = os.environ.get('PYTHONPATH', '')
        pypath = f'{REPO_ROOT}{os.pathsep}{existing_pypath}' if existing_pypath else str(REPO_ROOT)
        env = {
            **os.environ,
            'PYTHONPATH': pypath,
            'PYTHONUTF8': '1',
            'PYTHONIOENCODING': 'utf-8',
            'LLMCODE_ENGINE_KEY': api_key,
            'LITELLM_MASTER_KEY': api_key,
            'LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY': 'true',
            # LiteLLM >=1.10x routes openai/ Anthropic traffic via /v1/responses, which skips the
            # system-message merge that Qwen-style templates need; force chat/completions.
            'LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES': 'true',
        }
        with open(PROXY_LOG_FILE, 'a', encoding='utf-8') as log:
            _proxy['proc'] = subprocess.Popen(
                [str(exe), '--config', str(PROXY_CONFIG_FILE), '--host', '127.0.0.1', '--port', str(port)],
                stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=(sys.platform != 'win32'),
            )
        _proxy['signature'] = signature
        _proxy['port'] = port
    _wait_ready(port)
    return f'http://127.0.0.1:{port}'


def _wait_ready(port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = _proxy['proc']
        if p is None or p.poll() is not None:
            raise RuntimeError(f'LiteLLM proxy exited early; see {PROXY_LOG_FILE}')
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health/liveliness', timeout=2):
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f'LiteLLM proxy not ready on port {port} after {int(timeout)}s; see {PROXY_LOG_FILE}')


def _stop_locked() -> None:
    p = _proxy['proc']
    if p is not None and p.poll() is None:
        from src.common.process import kill_process_tree
        kill_process_tree(p.pid)
    _proxy.update(proc=None, signature=None, port=None)


def stop_litellm_proxy() -> None:
    """Stops the proxy; called when the engine stops so no orphan keeps the old API key."""
    with _lock:
        _stop_locked()


def proxy_status() -> Dict[str, Optional[int]]:
    p = _proxy['proc']
    running = p is not None and p.poll() is None
    return {'running': running, 'port': _proxy['port'] if running else None}


def resolve_base_url(style: str, engine_port: int, alias: str, api_key: str) -> Tuple[str, str]:
    """Returns (base_url, mode) where mode is 'native' or 'proxy'."""
    engine_url = f'http://127.0.0.1:{engine_port}'
    if probe_native(engine_url, style, alias, api_key):
        return engine_url, 'native'
    return ensure_litellm_proxy(engine_url, engine_port, alias, api_key), 'proxy'
