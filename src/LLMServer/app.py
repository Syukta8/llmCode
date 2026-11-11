import concurrent.futures
import json, os, re, secrets, struct, subprocess, threading, time, urllib.request, urllib.parse, urllib.error, shutil, signal, uuid
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

import sys
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.paths import (
    API_KEY_FILE,
    AGENT_ENV_FILE,
    CONFIG_DIR as STORE,
    CONFIG_FILE,
    DAEMON_LOG_FILE,
    DEPLOY_STATE_FILE,
    HARDWARE_FILE,
    MODEL_DIR,
    LOG_FILE,
    build_binary_env,
    get_llama_server_binary,
    get_koboldcpp_binary,
    get_ollama_binary,
    get_standard_alias,
    load_config,
    load_hardware,
    save_config,
    sync_config,
    write_secret,
)
from src.common.process import detect_terminal, spawn_in_terminal, kill_process_tree
from src.common.rag import (
    RAG_CONTEXT_FILE,
    RAG_INDEX_FILE,
    generate_rag_context_file,
    index_workspace,
    retrieve_chunks,
)
from src.common.harness import (
    HARNESSES,
    get_harness,
    build_launch_command,
    context_warning,
    CODEX_PROFILE,
)
from src.common.harness_config import (
    write_codex_profile,
    write_claude_settings,
    write_claude_mcp_config,
    get_configured_mcp_servers,
    DEFAULT_MCP_CATALOG,
    DEFAULT_MCP_SERVERS,
)
from src.common.endpoint import resolve_base_url, stop_litellm_proxy, proxy_status

# Auto-verify and sync paths in config/config.json on server startup
_initial_cfg = sync_config()

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get('LLAMA_PORT', str(_initial_cfg.get('settings', {}).get('server_port', 8080))))
lock = threading.RLock()
proc = None
log_handle = None
harness_procs: Dict[str, List[Dict[str, Any]]] = {
    'opencode': [],
    'claude': [],
    'codex': [],
}
opencode_proc = None
opencode_procs: List[Dict[str, Any]] = harness_procs['opencode']
_binary_flags_cache: Dict[str, Tuple[float, Set[str]]] = {}


def _prune_agent_procs(proc_list: List[Dict[str, Any]], legacy_proc: Any = None) -> List[Dict[str, Any]]:
    """Filters active agent processes whose Popen handles are still alive."""
    alive_list: List[Dict[str, Any]] = []
    seen_pids: Set[int] = set()
    for item in proc_list:
        p = item.get('proc')
        if p is not None and p.poll() is None:
            alive_list.append(item)
            if getattr(p, 'pid', None):
                seen_pids.add(p.pid)
    if legacy_proc is not None and legacy_proc.poll() is None and getattr(legacy_proc, 'pid', None) not in seen_pids:
        alive_list.append({
            'pid': legacy_proc.pid,
            'proc': legacy_proc,
            'project_dir': '',
            'started_at': '',
        })
    return alive_list


def get_binary_supported_flags(bin_path: Path) -> Set[str]:
    """Probes `<binary> --help` once per mtime and returns supported CLI flags."""
    if not bin_path or not bin_path.is_file():
        return set()
    key = str(bin_path.resolve())
    try:
        mtime = bin_path.stat().st_mtime
    except Exception:
        mtime = 0.0
    cached = _binary_flags_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    env = build_binary_env(bin_path)
    bin_parent = str(bin_path.resolve().parent)
    cmd = [sys.executable, key, '--help'] if bin_path.suffix.lower() == '.py' else [key, '--help']
    flags: Set[str] = set()
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5, cwd=bin_parent, env=env)
        out = (res.stdout or '') + '\n' + (res.stderr or '')
        for token in re.findall(r'(?<!\w)(--[a-zA-Z0-9][a-zA-Z0-9_-]*|-[a-zA-Z0-9]+)', out):
            flags.add(token)
    except Exception:
        pass
    _binary_flags_cache[key] = (mtime, flags)
    return flags


def strip_incompatible_parallel_flags(custom_args: List[str], parallel_slots: int) -> Tuple[List[str], bool]:
    """Removes `--spec-type draft-mtp` (and standalone `--spec-type`) when `parallel_slots > 1`."""
    if parallel_slots <= 1 or not custom_args:
        return list(custom_args), False
    cleaned: List[str] = []
    stripped = False
    i = 0
    while i < len(custom_args):
        arg = custom_args[i]
        if arg == '--spec-type':
            stripped = True
            if i + 1 < len(custom_args) and not custom_args[i + 1].startswith('-'):
                i += 2
            else:
                i += 1
            continue
        if arg.startswith('--spec-type='):
            stripped = True
            i += 1
            continue
        cleaned.append(arg)
        i += 1
    return cleaned, stripped


def sanitize_managed_custom_flags(
    custom_args: List[str],
    strip_spec: bool = True,
    strip_mmproj: bool = True,
) -> List[str]:
    """
    Removes CLI flags managed by structured MTP and mmproj controls so legacy `custom_flags`
    do not pass duplicate `--spec-type` or stale `--mmproj` paths from a previously selected model.
    """
    if not custom_args:
        return []
    spec_opts = {
        '--spec-type',
        '--spec-draft-n-max',
        '--spec-draft-n-min',
        '--spec-draft-p-min',
        '--draft-p-min',
        '--draft-max',
        '--draft',
    }
    mmproj_opts = {'--mmproj'}
    cleaned: List[str] = []
    i = 0
    while i < len(custom_args):
        arg = custom_args[i]
        if strip_spec and (arg in spec_opts or any(arg.startswith(f'{o}=') for o in spec_opts)):
            if '=' not in arg and i + 1 < len(custom_args) and not custom_args[i + 1].startswith('-'):
                i += 2
            else:
                i += 1
            continue
        if strip_mmproj and (arg in mmproj_opts or any(arg.startswith(f'{o}=') for o in mmproj_opts)):
            if '=' not in arg and i + 1 < len(custom_args) and not custom_args[i + 1].startswith('-'):
                i += 2
            else:
                i += 1
            continue
        cleaned.append(arg)
        i += 1
    return cleaned


def load_downloads():
    """Load persisted downloads state from config/config.json."""
    cfg = load_config()
    dl = cfg.get('downloads', {})
    changed = False
    for d in dl.values():
        if isinstance(d, dict) and d.get('state') == 'downloading':
            d['state'] = 'error'
            d['error'] = 'Interrupted by server restart'
            changed = True
    if changed:
        cfg['downloads'] = dl
        save_config(cfg)
    return dl


downloads = load_downloads()
runtime_state = {'active': None, 'pid': None}
app = FastAPI(title='llmCode', docs_url='/api/docs', openapi_url='/api/openapi.json')


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Logs validation errors to llama-server.log and returns a clean human-readable message."""
    msgs = []
    for err in exc.errors():
        loc = '.'.join(str(x) for x in err.get('loc', []) if x != 'body')
        msg = err.get('msg', 'invalid value')
        inp = err.get('input', '')
        msgs.append(f"{loc}: {msg} (got {inp!r})")
    summary = '; '.join(msgs) or str(exc)
    append_log(f"[VALIDATION ERROR] {request.url.path} -> {summary}")
    return JSONResponse(status_code=422, content={'detail': summary, 'errors': exc.errors()})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Logs HTTPExceptions to llama-server.log so UI Console Logs always record the failure reason."""
    append_log(f"[HTTP {exc.status_code}] {request.url.path} -> {exc.detail}")
    return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})


def save_downloads():
    """Persist downloads state into config/config.json."""
    try:
        cfg = load_config()
        cfg['downloads'] = downloads
        save_config(cfg)
    except Exception:
        pass


def load_settings():
    """Load persisted settings from config/config.json, automatically purging any legacy permanent api_key."""
    cfg = load_config()
    st = cfg.get('settings', {})
    if 'api_key' in st:
        st.pop('api_key', None)
        save_config(cfg)
    return st


_current_api_key: Optional[str] = None
_key_rotated_at: Optional[str] = None


def rotate_api_key() -> str:
    """Generates a fresh random API key on each deploy and saves to restricted config/api_key and config/agent.env."""
    global _current_api_key, _key_rotated_at
    key = f"sk-{secrets.token_urlsafe(32)}"
    write_secret(API_KEY_FILE, key)
    agent_env_content = f"OPENAI_API_KEY={key}\nOPENAI_API_BASE=http://127.0.0.1:{PORT}/v1\nOPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1\n"
    write_secret(AGENT_ENV_FILE, agent_env_content)
    _current_api_key = key
    _key_rotated_at = datetime.now().isoformat(timespec='seconds')
    return key


def get_current_api_key() -> Optional[str]:
    """Retrieves active API key from memory or config/api_key."""
    global _current_api_key
    if _current_api_key:
        return _current_api_key
    if API_KEY_FILE.is_file():
        try:
            k = API_KEY_FILE.read_text(encoding='utf-8').strip()
            if k:
                _current_api_key = k
                return k
        except Exception:
            pass
    return None


def clear_api_key() -> None:
    """Removes active API key and deletes credentials from disk."""
    global _current_api_key, _key_rotated_at
    _current_api_key = None
    _key_rotated_at = None
    for f in (API_KEY_FILE, AGENT_ENV_FILE):
        if f.is_file():
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


def get_or_create_api_key() -> str:
    """Backward-compatible wrapper returning current active API key, generating one if not yet initialized."""
    key = get_current_api_key()
    if not key:
        key = rotate_api_key()
    return key


def mask_command_args(args: list) -> list:
    """Masks sensitive credential values in logged CLI commands so secrets never leak into logs."""
    masked = []
    skip_next = False
    for a in args:
        if skip_next:
            masked.append('***')
            skip_next = False
            continue
        masked.append(a)
        if a in ('--api-key', '--password', '--api-key-file'):
            skip_next = True
    return masked



def save_settings(data):
    """Persist settings into config/config.json."""
    cfg = load_config()
    cfg.setdefault('settings', {}).update(data)
    save_config(cfg)


def append_log(message: str):
    """Writes a timestamped diagnostic event to LOG_FILE so UI console logs always show activity and errors."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except Exception:
        pass


class Deploy(BaseModel):
    path: str
    backend: str = Field(default='llama.cpp', pattern=r'^(llama\.cpp|koboldcpp|ollama)$')
    alias: str = Field(default='llamacpp', pattern=r'^[a-zA-Z0-9_.-]{1,90}$')
    layers: int = Field(default=8, ge=0, le=999)
    context: int = Field(default=8192, ge=512, le=1048576)
    parallel: int = Field(default=1, ge=1, le=32)
    threads: int = Field(default=4, ge=1, le=256)
    threads_batch: int = Field(default=6, ge=1, le=256)
    batch: int = Field(default=2048, ge=32, le=65536)
    ubatch: int = Field(default=256, ge=32, le=16384)
    reasoning: bool = True
    flash_attn: str = Field(default='on', pattern='^(on|off)$')
    kv_cache: str = Field(default='q8_0', pattern='^(f16|q8_0|q4_0)$')
    context_extend: str = Field(default='none', pattern=r'^(none|linear_2x|yarn_2x|yarn_4x|yarn_8x)$')
    rag_enabled: bool = False
    temp: float = Field(default=0.2, ge=0.0, le=2.0)
    top_p: float = Field(default=0.95, ge=0.0, le=1.0)
    top_k: int = Field(default=40, ge=0, le=500)
    min_p: float = Field(default=0.05, ge=0.0, le=1.0)
    repeat_penalty: float = Field(default=1.05, ge=0.0, le=3.0)
    main_gpu: int = Field(default=0, ge=0, le=16)
    split_mode: str = Field(default='layer', pattern='^(none|layer|row)$')
    lan_access: bool = False
    mtp_mode: str = Field(default='auto', pattern=r'^(auto|on|off)$')
    spec_draft_n_max: int = Field(default=16, ge=1, le=64)
    spec_draft_p_min: float = Field(default=0.75, ge=0.0, le=1.0)
    mmproj_mode: str = Field(default='auto', pattern=r'^(auto|off)$')
    custom_flags: str = ''


class Download(BaseModel):
    repo: str
    filename: str


class RAGIndexRequest(BaseModel):
    project_dir: str = ''


class RAGQueryRequest(BaseModel):
    project_dir: str = ''
    query: str = ''
    top_k: int = Field(default=6, ge=1, le=30)


class HarnessLaunch(BaseModel):
    harness: str = Field(default='opencode', pattern=r'^(opencode|claude|codex)$')
    project_dir: str
    model_path: str = ''
    backend: str = Field(default='llama.cpp', pattern=r'^(llama\.cpp|koboldcpp|ollama)$')
    alias: str = Field(default='llamacpp', pattern=r'^[a-zA-Z0-9_.-]{1,90}$')
    layers: int = Field(default=8, ge=0, le=999)
    context: int = Field(default=8192, ge=512, le=1048576)
    parallel: int = Field(default=1, ge=1, le=32)
    threads: int = Field(default=4, ge=1, le=256)
    threads_batch: int = Field(default=6, ge=1, le=256)
    batch: int = Field(default=2048, ge=32, le=65536)
    ubatch: int = Field(default=256, ge=32, le=16384)
    reasoning: bool = True
    flash_attn: str = Field(default='on', pattern='^(on|off)$')
    kv_cache: str = Field(default='q8_0', pattern='^(f16|q8_0|q4_0)$')
    context_extend: str = Field(default='none', pattern=r'^(none|linear_2x|yarn_2x|yarn_4x|yarn_8x)$')
    rag_enabled: bool = False
    temp: float = Field(default=0.2, ge=0.0, le=2.0)
    top_p: float = Field(default=0.95, ge=0.0, le=1.0)
    top_k: int = Field(default=40, ge=0, le=500)
    min_p: float = Field(default=0.05, ge=0.0, le=1.0)
    repeat_penalty: float = Field(default=1.05, ge=0.0, le=3.0)
    main_gpu: int = Field(default=0, ge=0, le=16)
    split_mode: str = Field(default='layer', pattern='^(none|layer|row)$')
    lan_access: bool = False
    mtp_mode: str = Field(default='auto', pattern=r'^(auto|on|off)$')
    spec_draft_n_max: int = Field(default=16, ge=1, le=64)
    spec_draft_p_min: float = Field(default=0.75, ge=0.0, le=1.0)
    mmproj_mode: str = Field(default='auto', pattern=r'^(auto|off)$')
    custom_flags: str = ''
    restore_history: bool = False
    session_id: str = ''


class OpenCodeLaunch(HarnessLaunch):
    pass


class SequenceTaskItem(BaseModel):
    id: str = ''
    prompt: str
    system: str = ''
    max_tokens: int = Field(default=1024, ge=1, le=32768)
    temp: Optional[float] = Field(default=None, ge=0.0, le=2.0)


class SequenceRunRequest(BaseModel):
    items: List[SequenceTaskItem] = Field(..., min_length=1, max_length=64)
    max_concurrency: int = Field(default=0, ge=0, le=32)
    join_policy: str = Field(default='all_required', pattern=r'^(all_required|best_effort)$')
    timeout_sec: int = Field(default=300, ge=5, le=3600)


def safe_model_path(path):
    p = Path(path).expanduser().resolve()
    if not p.is_relative_to(MODEL_DIR) or not p.is_file() or p.suffix.lower() != '.gguf':
        raise HTTPException(400, 'Choose an existing GGUF from the configured model directory')
    return p


def alive():
    global proc
    return bool(proc is not None and proc.poll() is None)


def http_json(url, timeout=8):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'LocalLLM-Control-Center/0.1'}), timeout=timeout) as r:
        return json.loads(r.read(3_000_000))


def server_health():
    curr_key = get_current_api_key()
    headers = {'Authorization': f'Bearer {curr_key}'} if curr_key else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{PORT}/health', headers=headers), timeout=2) as r:
            return json.loads(r.read(3_000_000))
    except Exception:
        pass
    try:
        with urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{PORT}/v1/models', headers=headers), timeout=2) as r:
            return json.loads(r.read(3_000_000))
    except Exception:
        return None


def system_status():
    global harness_procs, opencode_proc
    gpu = {'available': False}
    try:
        if sys.platform == 'darwin':
            res = subprocess.run(['system_profiler', 'SPDisplaysDataType'], capture_output=True, text=True, timeout=4)
            if res.returncode == 0:
                m = re.search(r'Chipset Model:\s*(.+)', res.stdout)
                name = m.group(1).strip() if m else 'Apple Metal GPU'
                gpu = {'available': True, 'name': name, 'total_mb': 0, 'used_mb': 0, 'util_pct': 0, 'temperature_c': 0}
        else:
            smi_bin = shutil.which('nvidia-smi')
            if not smi_bin and os.name == 'nt':
                for c in (
                    Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'nvidia-smi.exe',
                    Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'NVIDIA Corporation' / 'NVSMI' / 'nvidia-smi.exe',
                ):
                    if c.is_file():
                        smi_bin = str(c)
                        break
            if smi_bin:
                r = subprocess.run(
                    [smi_bin, '--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu', '--format=csv,noheader,nounits'],
                    capture_output=True, text=True, timeout=4
                )
                if r.returncode == 0 and r.stdout.strip():
                    v = [x.strip() for x in r.stdout.splitlines()[0].split(',')]
                    if len(v) >= 5:
                        def _safe_int(val: str) -> int:
                            m_i = re.search(r'(\d+)', val or '')
                            return int(m_i.group(1)) if m_i else 0
                        gpu = {
                            'available': True,
                            'name': v[0],
                            'total_mb': _safe_int(v[1]),
                            'used_mb': _safe_int(v[2]),
                            'util_pct': _safe_int(v[3]),
                            'temperature_c': _safe_int(v[4]),
                        }
    except Exception:
        pass
    if not gpu.get('available'):
        hw_gpu = load_hardware().get('gpu', {})
        if hw_gpu.get('available'):
            gpu = {
                'available': True,
                'name': hw_gpu.get('name', 'GPU'),
                'total_mb': int(hw_gpu.get('vram_mb', 0)),
                'used_mb': 0,
                'util_pct': 0,
                'temperature_c': 0,
            }

    mem = {}
    try:
        if os.name == 'nt':
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong),
                    ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('sullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                mem = {
                    'total_mb': int(stat.ullTotalPhys // (1024 * 1024)),
                    'available_mb': int(stat.ullAvailPhys // (1024 * 1024))
                }
        elif sys.platform == 'darwin':
            total_b = int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True, timeout=2).strip())
            vm_out = subprocess.check_output(['vm_stat'], text=True, timeout=2)
            free_pages = 0
            for line in vm_out.splitlines():
                if 'Pages free' in line or 'Pages inactive' in line or 'Pages speculative' in line:
                    m = re.search(r':\s*(\d+)', line)
                    if m:
                        free_pages += int(m.group(1))
            mem = {
                'total_mb': total_b // (1024 * 1024),
                'available_mb': (free_pages * 4096) // (1024 * 1024),
            }
        else:
            rows = {}
            for line in Path('/proc/meminfo').read_text().splitlines():
                k, _, v = line.partition(':')
                rows[k] = int(v.strip().split()[0])
            mem = {'total_mb': rows['MemTotal'] // 1024, 'available_mb': rows['MemAvailable'] // 1024}
    except Exception:
        pass

    server_bin = get_llama_server_binary()
    total_active_sessions = 0
    for hid, procs in harness_procs.items():
        legacy = opencode_proc if hid == 'opencode' else None
        alive_procs = _prune_agent_procs(procs, legacy)
        harness_procs[hid] = alive_procs
        total_active_sessions += len(alive_procs)
    return {
        'gpu': gpu,
        'ram': mem,
        'binary': str(server_bin),
        'binary_exists': server_bin.is_file(),
        'model_dir': str(MODEL_DIR),
        'model_dir_exists': MODEL_DIR.is_dir(),
        'config_file': str(CONFIG_FILE),
        'server_port': PORT,
        'api_key': get_current_api_key(),
        'key_rotated_at': _key_rotated_at,
        'agents_running': total_active_sessions > 0,
        'agent_session_count': total_active_sessions,
        'platform': sys.platform,
    }


@app.get('/', response_class=HTMLResponse)
def index():
    return (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')


_status_cache = {'health': None, 'system': {'gpu': {'available': False}, 'ram': {}}}
_status_cache_lock = threading.Lock()


def _status_poller(interval=3.0):
    """Refreshes slow probes off the request path so /api/status never blocks on llama-server or nvidia-smi."""
    while True:
        try:
            health = server_health()
            system = system_status()
            with _status_cache_lock:
                _status_cache['health'] = health
                _status_cache['system'] = system
        except Exception:
            pass
        time.sleep(interval)


threading.Thread(target=_status_poller, daemon=True, name='status-poller').start()


@app.get('/api/status')
def status():
    with lock:
        running = alive()
        managed_pid = proc.pid if running else None
        active = runtime_state.get('active') if running else None
    with _status_cache_lock:
        health = _status_cache['health']
        system = dict(_status_cache['system'])
    return {
        'managed_running': running,
        'managed_pid': managed_pid,
        'active': active,
        'health': health,
        **system,
    }


@app.get('/api/hardware')
def hardware():
    """Returns scanned hardware profile and build/runtime recommendations from config/hardware.json."""
    return load_hardware()


_gguf_meta_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def find_sibling_mmproj(model_path: Path) -> Optional[Dict[str, Any]]:
    """Locates a sibling mmproj*.gguf file in the same directory as `model_path`."""
    try:
        parent = Path(model_path).resolve().parent
        if not parent.is_dir():
            return None
        for cand in sorted(parent.glob('*.gguf')):
            if cand.is_file() and 'mmproj' in cand.name.lower() and cand.resolve() != Path(model_path).resolve():
                st = cand.stat()
                return {
                    'path': str(cand),
                    'name': cand.name,
                    'size_gb': round(st.st_size / 1e9, 2),
                    'size_bytes': st.st_size,
                }
    except Exception:
        pass
    return None


def get_gguf_metadata(model_path: Path) -> Dict[str, Any]:
    """
    Extracts architecture, native context_length, nextn_predict_layers (MTP), block_count,
    and attention dimensions from GGUF binary header without loading weights into RAM.
    """
    default_meta: Dict[str, Any] = {
        'architecture': '',
        'context_length': 0,
        'nextn_predict_layers': 0,
        'block_count': 0,
        'head_count_kv': 0,
        'key_length': 128,
        'value_length': 128,
        'full_attention_interval': 0,
        'sibling_mmproj': None,
    }
    try:
        p = Path(model_path).resolve()
        if not p.is_file() or p.stat().st_size < 32:
            return default_meta
        mtime = p.stat().st_mtime
        cache_key = str(p)
        cached = _gguf_meta_cache.get(cache_key)
        if cached and cached[0] == mtime:
            res = dict(cached[1])
            res['sibling_mmproj'] = find_sibling_mmproj(p)
            return res

        raw_kv: Dict[str, Any] = {}
        with open(p, 'rb') as f:
            if f.read(4) != b'GGUF':
                return default_meta
            version = struct.unpack('<I', f.read(4))[0]
            if version not in (2, 3):
                return default_meta
            _tensor_count = struct.unpack('<Q', f.read(8))[0]
            kv_count = struct.unpack('<Q', f.read(8))[0]
            scalar_sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
            for _ in range(min(kv_count, 1024)):
                klen_bytes = f.read(8)
                if len(klen_bytes) < 8:
                    break
                klen = struct.unpack('<Q', klen_bytes)[0]
                if klen > 1024:
                    break
                key = f.read(klen).decode('utf-8', errors='ignore')
                vtype_bytes = f.read(4)
                if len(vtype_bytes) < 4:
                    break
                vtype = struct.unpack('<I', vtype_bytes)[0]
                if vtype in (4, 5):
                    val_b = f.read(4)
                    if len(val_b) < 4:
                        break
                    raw_kv[key] = struct.unpack('<I' if vtype == 4 else '<i', val_b)[0]
                elif vtype in (10, 11):
                    val_b = f.read(8)
                    if len(val_b) < 8:
                        break
                    raw_kv[key] = struct.unpack('<Q' if vtype == 10 else '<q', val_b)[0]
                elif vtype == 8:
                    slen_b = f.read(8)
                    if len(slen_b) < 8:
                        break
                    slen = struct.unpack('<Q', slen_b)[0]
                    if key == 'general.architecture' and slen <= 256:
                        raw_kv[key] = f.read(slen).decode('utf-8', errors='ignore')
                    else:
                        f.seek(slen, os.SEEK_CUR)
                elif vtype in scalar_sizes:
                    f.seek(scalar_sizes[vtype], os.SEEK_CUR)
                elif vtype == 9:
                    atype_b = f.read(4)
                    alen_b = f.read(8)
                    if len(atype_b) < 4 or len(alen_b) < 8:
                        break
                    atype = struct.unpack('<I', atype_b)[0]
                    alen = struct.unpack('<Q', alen_b)[0]
                    sz = scalar_sizes.get(atype)
                    if sz:
                        f.seek(sz * alen, os.SEEK_CUR)
                    elif atype == 8:
                        for _ in range(alen):
                            slen_b = f.read(8)
                            if len(slen_b) < 8:
                                break
                            slen = struct.unpack('<Q', slen_b)[0]
                            f.seek(slen, os.SEEK_CUR)
                    else:
                        break
                else:
                    break

        arch = str(raw_kv.get('general.architecture', '') or '')
        meta = dict(default_meta)
        meta['architecture'] = arch
        if arch:
            meta['context_length'] = int(raw_kv.get(f'{arch}.context_length', 0) or 0)
            meta['nextn_predict_layers'] = int(raw_kv.get(f'{arch}.nextn_predict_layers', 0) or 0)
            meta['block_count'] = int(raw_kv.get(f'{arch}.block_count', 0) or 0)
            meta['head_count_kv'] = int(raw_kv.get(f'{arch}.attention.head_count_kv', 0) or 0)
            meta['key_length'] = int(raw_kv.get(f'{arch}.attention.key_length', 128) or 128)
            meta['value_length'] = int(raw_kv.get(f'{arch}.attention.value_length', 128) or 128)
            meta['full_attention_interval'] = int(raw_kv.get(f'{arch}.full_attention_interval', 0) or 0)
        _gguf_meta_cache[cache_key] = (mtime, dict(meta))
        meta['sibling_mmproj'] = find_sibling_mmproj(p)
        return meta
    except Exception:
        return default_meta


def get_gguf_architecture(model_path: Path) -> str:
    """Extracts general.architecture string from GGUF binary header."""
    return get_gguf_metadata(model_path).get('architecture', '')


@app.get('/api/models')
def models():
    found = []
    if MODEL_DIR.exists():
        for p in sorted(MODEL_DIR.rglob('*.gguf')):
            if p.is_file():
                st = p.stat()
                is_mmproj = 'mmproj' in p.name.lower()
                meta = get_gguf_metadata(p) if not is_mmproj else {}
                found.append({
                    'path': str(p),
                    'name': p.name,
                    'relative': p.relative_to(MODEL_DIR).as_posix(),
                    'size_gb': round(st.st_size / 1e9, 2),
                    'size_bytes': st.st_size,
                    'modified': datetime.fromtimestamp(st.st_mtime).isoformat(timespec='minutes'),
                    'mmproj': is_mmproj,
                    'architecture': meta.get('architecture', ''),
                    'context_length': meta.get('context_length', 0),
                    'nextn_predict_layers': meta.get('nextn_predict_layers', 0),
                    'block_count': meta.get('block_count', 0),
                    'head_count_kv': meta.get('head_count_kv', 0),
                    'key_length': meta.get('key_length', 128),
                    'value_length': meta.get('value_length', 128),
                    'full_attention_interval': meta.get('full_attention_interval', 0),
                    'sibling_mmproj': meta.get('sibling_mmproj'),
                })
            if len(found) >= 500:
                break
    return {'models': found, 'directory': str(MODEL_DIR)}


def build_context_extension_flags(backend: str, preset: str, orig_ctx: int = 0) -> list:
    """
    Translates Context Extension preset ('none', 'linear_2x', 'yarn_2x', 'yarn_4x', 'yarn_8x')
    into backend-specific RoPE / YaRN CLI flags, including `--yarn-orig-ctx` when native context is known.
    """
    if not preset or preset == 'none':
        return []
    scale_map = {
        'linear_2x': ('linear', 2, '0.5'),
        'yarn_2x': ('yarn', 2, '0.5'),
        'yarn_4x': ('yarn', 4, '0.25'),
        'yarn_8x': ('yarn', 8, '0.125'),
    }
    if preset not in scale_map:
        return []
    stype, factor, inv_scale = scale_map[preset]

    if backend == 'koboldcpp':
        return ['--ropeconfig', inv_scale, '10000']
    elif backend == 'llama.cpp':
        flags = ['--rope-scaling', stype, '--rope-scale', str(factor)]
        if stype == 'yarn':
            flags.extend(['--yarn-ext-factor', '-1.0'])
            if orig_ctx > 0:
                flags.extend(['--yarn-orig-ctx', str(orig_ctx)])
        return flags
    return []


def launch_backend_process(backend, path, params):
    """Launch llama.cpp, koboldcpp, or ollama on port PORT with standard alias, parallel slot scaling, and binary flag probing."""
    global proc, log_handle
    sync_config()
    server_bin = get_llama_server_binary()
    kobold_bin = get_koboldcpp_binary()
    ollama_bin = get_ollama_binary()

    # Standardize alias to llamacpp / koboldcpp / ollama unless user explicitly customized it
    std_alias = get_standard_alias(backend)
    if not getattr(params, 'alias', '') or params.alias in ('local-qwen', 'llamacpp', 'koboldcpp', 'ollama'):
        params.alias = std_alias

    parallel_slots = max(1, int(getattr(params, 'parallel', 1) or 1))
    per_slot_ctx = int(getattr(params, 'context', 8192) or 8192)
    effective_ctx = per_slot_ctx * parallel_slots

    meta = get_gguf_metadata(path)
    arch = meta.get('architecture', '')
    orig_ctx = int(meta.get('context_length', 0) or 0)
    nextn_layers = int(meta.get('nextn_predict_layers', 0) or 0)
    sibling_mmproj = meta.get('sibling_mmproj')

    ctx_ext = getattr(params, 'context_extend', 'none') or 'none'
    rag_on = bool(getattr(params, 'rag_enabled', False))
    mtp_mode = getattr(params, 'mtp_mode', 'auto') or 'auto'
    spec_draft_n_max = int(getattr(params, 'spec_draft_n_max', 16) or 16)
    spec_draft_p_min = float(getattr(params, 'spec_draft_p_min', 0.75) if getattr(params, 'spec_draft_p_min', None) is not None else 0.75)
    mmproj_mode = getattr(params, 'mmproj_mode', 'auto') or 'auto'

    ext_flags = build_context_extension_flags(backend, ctx_ext, orig_ctx=orig_ctx)
    raw_custom_args = [arg for arg in params.custom_flags.split() if arg.strip()] if getattr(params, 'custom_flags', None) else []

    want_mtp = (mtp_mode == 'on') or (mtp_mode == 'auto' and nextn_layers > 0)
    mtp_active = False
    stripped_spec = False
    if backend == 'llama.cpp' and want_mtp:
        if parallel_slots > 1:
            stripped_spec = True
            append_log(f"[INFO] Auto-disabled draft-mtp because parallel={parallel_slots} > 1 (incompatible with multi-slot continuous batching)")
        else:
            mtp_active = True

    # Strip managed --spec-type / --mmproj from custom_flags so structured controls and current model sibling mmproj win cleanly
    sanitized_custom = sanitize_managed_custom_flags(
        raw_custom_args,
        strip_spec=(backend == 'llama.cpp'),
        strip_mmproj=(backend == 'llama.cpp'),
    )
    custom_args, legacy_stripped = strip_incompatible_parallel_flags(sanitized_custom, parallel_slots)
    stripped_spec = stripped_spec or legacy_stripped

    key = rotate_api_key()
    lan = bool(getattr(params, 'lan_access', False))
    bind_host = '127.0.0.1' if backend == 'ollama' or not lan else '0.0.0.0'

    if backend == 'koboldcpp':
        if not kobold_bin.is_file():
            raise HTTPException(400, f'koboldcpp not found at {kobold_bin}')
        supported = get_binary_supported_flags(kobold_bin)
        base_cmd = [sys.executable, str(kobold_bin)] if kobold_bin.suffix.lower() == '.py' else [str(kobold_bin)]
        args = base_cmd + [
            '--model', str(path),
            '--port', str(PORT),
            '--contextsize', str(effective_ctx),
            '--gpulayers', str(params.layers),
            '--threads', str(params.threads),
            '--blasbatchsize', str(params.batch),
            '--usemmap',
            '--skiplauncher', '--quiet', '--host', bind_host,
            '--password', key,
        ]
        flavor_file = kobold_bin.parent / 'koboldcpp.flavor'
        flavor = flavor_file.read_text(encoding='utf-8').strip().lower() if flavor_file.is_file() else ''
        can_use_cuda = (
            params.layers > 0
            and sys.platform != 'darwin'
            and flavor != 'cpu'
            and 'nocuda' not in kobold_bin.name.lower()
            and (not supported or '--usecuda' in supported)
        )
        if can_use_cuda:
            args.append('--usecuda')
        if params.flash_attn == 'on' and (not supported or '--flashattention' in supported):
            args.append('--flashattention')
        if params.kv_cache == 'q8_0' and (not supported or '--quantkv' in supported):
            args.extend(['--quantkv', '1'])
        elif params.kv_cache == 'q4_0' and (not supported or '--quantkv' in supported):
            args.extend(['--quantkv', '2'])
        if parallel_slots > 1 and (not supported or '--multiuser' in supported):
            args.extend(['--multiuser', str(parallel_slots)])
        # KoboldCpp does not have a standalone --embeddings flag (only --embeddingsmodel).
        # We omit it to prevent CLI parameter errors.
        args.extend(ext_flags)
        args.extend(custom_args)

    elif backend == 'ollama':
        if not ollama_bin.is_file():
            raise HTTPException(400, f'ollama binary not found at {ollama_bin}')
        modelfile_path = STORE / 'Modelfile'
        modelfile_content = [
            f"FROM {path}",
            f"PARAMETER num_ctx {effective_ctx}",
            f"PARAMETER num_gpu {params.layers}",
            f"PARAMETER num_thread {params.threads}",
            f"PARAMETER temperature {params.temp}",
            f"PARAMETER top_p {params.top_p}",
            f"PARAMETER top_k {params.top_k}",
            f"PARAMETER repeat_penalty {params.repeat_penalty}",
        ]
        if getattr(params, 'reasoning', True):
            modelfile_content.append("PARAMETER think true")
        modelfile_path.write_text("\n".join(modelfile_content) + "\n", encoding='utf-8')

        # Register/create model in Ollama library so Continue and OpenAI endpoints recognise it
        ollama_model_name = getattr(params, 'alias', 'ollama') or 'ollama'
        try:
            create_cmd = [str(ollama_bin), 'create', ollama_model_name, '-f', str(modelfile_path)]
            subprocess.run(create_cmd, check=True, capture_output=True, timeout=120)
        except Exception as e:
            append_log(f"[WARN] Failed to pre-register model in Ollama: {e}")

        args = [str(ollama_bin), 'serve']
        args.extend(custom_args)

    else:  # default llama.cpp
        if not server_bin.is_file():
            raise HTTPException(400, f'llama-server not found at {server_bin}')
        supported = get_binary_supported_flags(server_bin)
        args = [
            str(server_bin), '--model', str(path), '--alias', params.alias,
            '--host', bind_host, '--port', str(PORT),
            '--api-key-file', str(API_KEY_FILE),
            '--ctx-size', str(effective_ctx),
            '--n-gpu-layers', str(params.layers),
            '--threads', str(params.threads),
            '--threads-batch', str(getattr(params, 'threads_batch', params.threads)),
            '-b', str(params.batch),
            '-ub', str(params.ubatch),
            '--temp', str(params.temp),
            '--top-p', str(params.top_p),
            '--top-k', str(params.top_k),
            '--min-p', str(params.min_p),
            '--repeat-penalty', str(params.repeat_penalty),
            '--parallel', str(parallel_slots),
            '--jinja',
            '-fa', params.flash_attn,
            '-sm', params.split_mode,
            '--main-gpu', str(params.main_gpu),
        ]
        if parallel_slots > 1:
            if not supported or '--cont-batching' in supported:
                args.append('--cont-batching')
            if '-kvu' in supported:
                args.append('-kvu')
            elif '--kv-unified' in supported:
                args.append('--kv-unified')
        if params.kv_cache != 'f16':
            args += ['-ctk', params.kv_cache, '-ctv', params.kv_cache]
        want_reasoning = not (hasattr(params, 'reasoning') and not params.reasoning)
        if not supported or '--reasoning' in supported:
            args += ['--reasoning', 'on' if want_reasoning else 'off']
        elif '--reasoning-budget' in supported:
            args += ['--reasoning-budget', '-1' if want_reasoning else '0']
        if rag_on:
            args += ['--embeddings', '--pooling', 'mean']
        args.extend(ext_flags)
        if effective_ctx > 0 and not any('--override-kv' in a for a in custom_args):
            if arch:
                args.extend(['--override-kv', f'{arch}.context_length=int:{effective_ctx}'])
        if mtp_active:
            args.extend([
                '--spec-type', 'draft-mtp',
                '--spec-draft-n-max', str(spec_draft_n_max),
                '--spec-draft-p-min', str(spec_draft_p_min),
            ])
            if ctx_ext != 'none' or (orig_ctx > 0 and effective_ctx > orig_ctx):
                append_log(
                    f"[WARN] Long-Context MTP Trap: draft-mtp active with extended context ({effective_ctx} > native {orig_ctx}, extend={ctx_ext}). "
                    f"Dense MTP attention scales quadratically and allocates 2x KV contexts."
                )
        if mmproj_mode == 'auto' and sibling_mmproj and sibling_mmproj.get('path'):
            args.extend(['--mmproj', str(sibling_mmproj['path'])])
        args.extend(custom_args)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if log_handle is not None:
        try:
            log_handle.close()
        except Exception:
            pass
    log_handle = open(LOG_FILE, 'a', buffering=1, encoding='utf-8')
    masked_args = mask_command_args(args)
    cmd_str = ' '.join(f'"{a}"' if ' ' in a else a for a in masked_args)
    log_handle.write(f'\n===== [{backend}] {datetime.now().isoformat(timespec="seconds")} =====\n')
    log_handle.write(f'[COMMAND] {cmd_str}\n')
    log_handle.flush()

    bin_target = Path(args[1] if len(args) > 1 and args[0] == sys.executable else args[0]).resolve()
    bin_parent = str(bin_target.parent)
    env = build_binary_env(bin_target)
    if backend == 'ollama':
        env['OLLAMA_HOST'] = f'127.0.0.1:{PORT}'
        env['OLLAMA_NUM_PARALLEL'] = str(parallel_slots)

    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=log_handle,
            stderr=subprocess.STDOUT, start_new_session=True, cwd=bin_parent, env=env
        )
    except Exception as e:
        append_log(f"[ERROR] Failed to spawn {backend}: {e}")
        raise HTTPException(500, f"Failed to spawn {backend}: {e}")

    # Wait briefly to catch immediate startup crashes (e.g. missing CUDA DLLs, invalid flags, OOM)
    time.sleep(0.35)
    if proc.poll() is not None:
        exit_code = proc.returncode
        append_log(f"[ERROR] {backend} exited immediately with code {exit_code} (0x{exit_code & 0xFFFFFFFF:08X})")
        raise HTTPException(
            500,
            f"{backend} exited immediately (code {exit_code}). Check Server Console Logs for details."
        )
    runtime_state['active'] = {
        'backend': backend,
        'name': path.name,
        'path': str(path),
        'alias': params.alias,
        'layers': params.layers,
        'context': per_slot_ctx,
        'parallel': parallel_slots,
        'effective_context': effective_ctx,
        'threads': params.threads,
        'threads_batch': getattr(params, 'threads_batch', params.threads),
        'batch': params.batch,
        'ubatch': params.ubatch,
        'flash_attn': params.flash_attn,
        'kv_cache': params.kv_cache,
        'context_extend': ctx_ext,
        'rag_enabled': rag_on,
        'temp': params.temp,
        'top_p': params.top_p,
        'top_k': params.top_k,
        'min_p': params.min_p,
        'repeat_penalty': params.repeat_penalty,
        'main_gpu': params.main_gpu,
        'split_mode': params.split_mode,
        'mtp_mode': mtp_mode,
        'mtp_active': mtp_active,
        'spec_draft_n_max': spec_draft_n_max,
        'spec_draft_p_min': spec_draft_p_min,
        'mmproj_mode': mmproj_mode,
        'mmproj_path': sibling_mmproj.get('path') if (mmproj_mode == 'auto' and sibling_mmproj) else None,
        'custom_flags': ' '.join(custom_args),
        'spec_auto_disabled': stripped_spec,
    }
    runtime_state['pid'] = proc.pid
    return args


@app.post('/api/start')
def start(params: Deploy):
    path = safe_model_path(params.path)
    with lock:
        if alive():
            raise HTTPException(409, 'A server started by this dashboard is already running. Stop it first.')
        args = launch_backend_process(params.backend, path, params)
        save_settings({
            'last_model_path': str(path),
            'last_backend': params.backend,
            'last_alias': params.alias,
            'last_layers': params.layers,
            'last_context': params.context,
            'last_parallel': params.parallel,
            'last_threads': params.threads,
            'last_threads_batch': params.threads_batch,
            'last_batch': params.batch,
            'last_ubatch': params.ubatch,
            'last_flash_attn': params.flash_attn,
            'last_kv_cache': params.kv_cache,
            'last_context_extend': params.context_extend,
            'last_rag_enabled': params.rag_enabled,
            'last_temp': params.temp,
            'last_top_p': params.top_p,
            'last_top_k': params.top_k,
            'last_min_p': params.min_p,
            'last_repeat_penalty': params.repeat_penalty,
            'last_main_gpu': params.main_gpu,
            'last_split_mode': params.split_mode,
            'last_lan_access': params.lan_access,
            'last_reasoning': params.reasoning,
            'last_mtp_mode': params.mtp_mode,
            'last_spec_draft_n_max': params.spec_draft_n_max,
            'last_spec_draft_p_min': params.spec_draft_p_min,
            'last_mmproj_mode': params.mmproj_mode,
            'last_custom_flags': ' '.join(sanitize_managed_custom_flags(params.custom_flags.split())),
        })
        write_opencode_config(None, params.alias, PORT, context_limit=params.context)
        return {'ok': True, 'pid': proc.pid, 'command': args, 'message': f'[{params.backend}] launching; health may take time to become ready.'}


@app.post('/api/stop')
def stop():
    global proc, log_handle
    with lock:
        stop_litellm_proxy()
        clear_api_key()
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass
            log_handle = None
        if not alive():
            return {'ok': True, 'message': 'No dashboard-managed server running'}
        pid = proc.pid
        kill_process_tree(pid)
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        return {'ok': True, 'message': f'Stopped dashboard-managed process {pid}'}


@app.get('/api/logs')
def logs(source: str = 'engine'):
    target_file = DAEMON_LOG_FILE if source == 'daemon' else LOG_FILE
    if not target_file.exists():
        return {'text': f'No {source} log yet ({target_file.name}).', 'file': str(target_file)}
    with open(target_file, 'rb') as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 32000))
        data = f.read()
    return {'text': data.decode('utf-8', 'replace')[-30000:], 'file': str(target_file)}


@app.post('/api/logs/clear')
def clear_logs(source: str = 'engine'):
    target_file = DAEMON_LOG_FILE if source == 'daemon' else LOG_FILE
    if target_file.exists():
        target_file.write_text('', encoding='utf-8')
    append_log(f"[INFO] Log cleared ({target_file.name})")
    return {'ok': True}


@app.get('/api/hf/search')
def hf_search(q: str = 'Qwen3.5', limit: int = 20):
    limit = max(1, min(limit, 50))
    query = urllib.parse.urlencode({'search': q, 'limit': limit, 'sort': 'downloads', 'direction': -1, 'filter': 'gguf'})
    try:
        result = http_json('https://huggingface.co/api/models?' + query, timeout=14)
        return {'models': [{'id': r.get('id'), 'downloads': r.get('downloads', 0), 'likes': r.get('likes', 0), 'private': r.get('private', False), 'gated': r.get('gated', False)} for r in result]}
    except Exception as e:
        raise HTTPException(502, f'Hugging Face query failed: {e}')


@app.get('/api/hf/files')
def hf_files(repo: str):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise HTTPException(400, 'Invalid repo id')
    try:
        result = http_json('https://huggingface.co/api/models/' + repo + '/tree/main?recursive=true&expand=false', timeout=20)
        files = [{'filename': f.get('path'), 'size_gb': round((f.get('size') or 0) / 1e9, 2), 'size_bytes': f.get('size') or 0} for f in result if f.get('path', '').lower().endswith('.gguf')]
        return {'files': files}
    except Exception as e:
        raise HTTPException(502, f'Hugging Face file listing failed: {e}')


@app.post('/api/hf/download')
def hf_download(d: Download):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', d.repo):
        raise HTTPException(400, 'Invalid repo id')
    if not d.filename.lower().endswith('.gguf') or '/' in d.filename or '\\' in d.filename:
        raise HTTPException(400, 'Invalid filename (must be GGUF filename without subdirectories)')

    dest_dir = MODEL_DIR / d.repo.replace('/', '__')
    dest_path = dest_dir / d.filename

    dl_id = f"{d.repo}:{d.filename}"
    with lock:
        if dl_id in downloads and downloads[dl_id]['state'] == 'downloading':
            raise HTTPException(409, 'Download already running')
        downloads[dl_id] = {
            'id': dl_id, 'repo': d.repo, 'filename': d.filename,
            'dest': str(dest_path), 'state': 'downloading',
            'received': 0, 'total': 0, 'speed_mb_s': 0,
            'started': datetime.now().isoformat(), 'error': None,
        }
        save_downloads()

    def worker():
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            part_path = dest_path.with_suffix(dest_path.suffix + '.part')
            url = f"https://huggingface.co/{d.repo}/resolve/main/{d.filename}"
            req = urllib.request.Request(url, headers={'User-Agent': 'llmCode/1.0'})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=30) as resp, open(part_path, 'wb') as f:
                total = int(resp.headers.get('Content-Length', 0))
                with lock:
                    downloads[dl_id]['total'] = total
                    save_downloads()
                buf_size = 1024 * 1024
                received = 0
                while True:
                    chunk = resp.read(buf_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    elapsed = max(0.1, time.time() - t0)
                    with lock:
                        downloads[dl_id]['received'] = received
                        downloads[dl_id]['speed_mb_s'] = round((received / elapsed) / 1e6, 2)
            part_path.rename(dest_path)
            with lock:
                downloads[dl_id]['state'] = 'complete'
                downloads[dl_id]['completed'] = datetime.now().isoformat()
                save_downloads()
        except Exception as e:
            with lock:
                downloads[dl_id]['state'] = 'error'
                downloads[dl_id]['error'] = str(e)
                save_downloads()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return {'ok': True, 'id': dl_id, 'message': 'Download started in background'}


@app.get('/api/downloads')
def list_downloads():
    with lock:
        return {'downloads': list(downloads.values())}


@app.get('/api/settings')
def get_settings():
    return load_settings()


@app.post('/api/rag/index')
def api_rag_index(req: RAGIndexRequest):
    """Indexes the specified workspace directory into config/rag_index.json & config/rag_context.md."""
    target = Path(req.project_dir or load_settings().get('last_project_dir', str(REPO_ROOT))).expanduser().resolve()
    if not target.is_dir():
        raise HTTPException(400, f'Workspace directory does not exist: {target}')
    res = index_workspace(target)
    append_log(f"[RAG] Indexed {res['file_count']} files ({res['chunk_count']} chunks) in {target}")
    return res


@app.post('/api/rag/query')
def api_rag_query(req: RAGQueryRequest):
    """Retrieves top relevant workspace chunks for a query and updates config/rag_context.md."""
    target = Path(req.project_dir or load_settings().get('last_project_dir', str(REPO_ROOT))).expanduser().resolve()
    if not target.is_dir():
        raise HTTPException(400, f'Workspace directory does not exist: {target}')
    ctx_file = generate_rag_context_file(target, query=req.query, top_k=req.top_k)
    chunks = retrieve_chunks(query=req.query, top_k=req.top_k, project_dir=target)
    return {
        'ok': True,
        'project_dir': str(target),
        'query': req.query,
        'chunks': chunks,
        'context_file': str(ctx_file),
    }


@app.get('/api/directories')
def list_directories(base: str = ''):
    current = Path(base).expanduser().resolve() if base else HOME
    if not current.is_dir():
        current = HOME
    dirs = []
    try:
        for entry in os.scandir(current):
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.'):
                dirs.append({'name': entry.name, 'path': entry.path})
    except PermissionError:
        pass
    dirs.sort(key=lambda d: d['name'].lower())
    return {
        'current': str(current),
        'parent': str(current.parent) if current.parent != current else str(current),
        'directories': dirs,
    }


@app.get('/api/harness/status')
def all_harness_status():
    global harness_procs, opencode_proc
    cfg = sync_config()
    agents_cfg = cfg.get('agents', {})
    res = {}
    for hid, hobj in HARNESSES.items():
        procs = harness_procs.setdefault(hid, [])
        legacy = opencode_proc if hid == 'opencode' else None
        alive_procs = _prune_agent_procs(procs, legacy)
        harness_procs[hid] = alive_procs
        running = len(alive_procs) > 0
        found_bin = agents_cfg.get(hid)
        res[hid] = {
            'id': hid,
            'label': hobj.label,
            'api_style': hobj.api_style,
            'running': running,
            'pid': alive_procs[-1]['pid'] if running else None,
            'session_count': len(alive_procs),
            'installed': bool(found_bin),
            'binary': found_bin,
            'active_sessions': [
                {'pid': s['pid'], 'project_dir': s.get('project_dir', ''), 'started_at': s.get('started_at', '')}
                for s in alive_procs
            ],
        }
    return {
        'harnesses': res,
        'proxy': proxy_status(),
        'terminal': detect_terminal(),
    }


@app.get('/api/harness/{harness_id}/status')
def harness_status(harness_id: str):
    if harness_id not in HARNESSES:
        raise HTTPException(400, f"Unknown harness '{harness_id}'")
    st = all_harness_status()
    return {**st['harnesses'][harness_id], 'terminal': st['terminal']}


@app.get('/api/opencode/status')
def opencode_status():
    return harness_status('opencode')


def ensure_git_ignored(project_dir: Path, filename: str = 'opencode.json') -> None:
    """Ensures configuration files with local paths or tokens are gitignored in the project directory."""
    git_dir = project_dir / '.git'
    gitignore_file = project_dir / '.gitignore'
    if git_dir.is_dir() or gitignore_file.is_file():
        existing = ''
        if gitignore_file.is_file():
            try:
                existing = gitignore_file.read_text(encoding='utf-8')
            except Exception:
                existing = ''
        lines = [line.strip() for line in existing.splitlines()]
        if filename not in lines and f'/{filename}' not in lines:
            prefix = '\n' if existing and not existing.endswith('\n') else ''
            try:
                with open(gitignore_file, 'a', encoding='utf-8') as f:
                    f.write(f"{prefix}{filename}\n")
            except Exception:
                pass


GLOBAL_OPENCODE_CONFIG = HOME / '.config' / 'opencode' / 'opencode.jsonc'


def write_opencode_config(
    project_dir: Optional[Path],
    alias: str,
    port: int,
    rag_context_file: Optional[Path] = None,
    context_limit: Optional[int] = None,
    mcp_servers: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Path:
    """
    Writes or updates the global OpenCode configuration (~/.config/opencode/opencode.jsonc)
    using the official OpenCode v2 schema (providers, permissions array, mcp.servers map).
    """
    cfg_path = GLOBAL_OPENCODE_CONFIG
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding='utf-8'))
        except Exception:
            data = {}
    data.setdefault('$schema', 'https://opencode.ai/config.json')

    # Prune legacy v1 keys
    data.pop('provider', None)
    data.pop('permission', None)

    providers = data.setdefault('providers', {})

    # Resolve active context limit
    ctx = context_limit
    if not ctx:
        try:
            st = json.loads(DEPLOY_STATE_FILE.read_text(encoding='utf-8'))
            ctx = int(st.get('last_context', 0))
        except Exception:
            ctx = 0
    if not ctx or ctx <= 0:
        ctx = 16384

    out_limit = min(4096, max(1024, ctx // 8))

    model_entry = {
        'name': alias,
        'capabilities': {
            'tools': True,
            'reasoning': {
                'supported': True,
                'effort': ['low', 'medium', 'high', 'xhigh'],
            },
        },
        'limit': {
            'context': ctx,
            'output': out_limit,
        },
        'variants': [
            {'id': 'low', 'settings': {'reasoningEffort': 'low'}},
            {'id': 'medium', 'settings': {'reasoningEffort': 'medium'}},
            {'id': 'high', 'settings': {'reasoningEffort': 'high'}},
            {'id': 'xhigh', 'settings': {'reasoningEffort': 'xhigh'}},
        ],
    }

    provider_entry = {
        'package': 'aisdk:@ai-sdk/openai-compatible',
        'name': f'llmCode ({alias})',
        'settings': {
            'baseURL': f'http://127.0.0.1:{port}/v1',
            'apiKey': f'{{file:{API_KEY_FILE.as_posix()}}}',
            'timeout': 600000,
        },
        'models': {
            alias: model_entry,
        },
    }

    providers['llmcode'] = provider_entry

    data['model'] = f'llmcode/{alias}'
    data['default_agent'] = 'build'
    data['permissions'] = [
        {'action': p, 'resource': '*', 'effect': 'allow'}
        for p in ('read', 'edit', 'glob', 'grep', 'list', 'shell', 'subagent', 'todowrite', 'question')
    ]

    instr_list = []
    global_instr = HOME / '.config' / 'opencode' / 'instructions.md'
    if global_instr.is_file():
        instr_list.append(str(global_instr.resolve()))
    if rag_context_file and rag_context_file.is_file():
        instr_list.append(str(rag_context_file.resolve()))
    if instr_list:
        data['instructions'] = instr_list

    # Modern OpenCode v2 mcp.servers map adhering to strict opt-in while supporting backward compatibility
    mcp_root = data.setdefault('mcp', {})
    # Strip deprecated root-level MCP server keys to avoid duplicate declarations
    for k in list(mcp_root.keys()):
        if k not in ('servers', 'timeout'):
            mcp_root.pop(k, None)

    servers_map = mcp_root.setdefault('servers', {})
    servers_map.clear()

    selected = mcp_servers if mcp_servers is not None else get_configured_mcp_servers()
    # Populate all standard catalog servers; mark active if in selected, else disabled for easy toggle
    for mcp_name, cat_spec in DEFAULT_MCP_CATALOG.items():
        is_active = bool(selected and mcp_name in selected)
        active_spec = selected.get(mcp_name, {}) if is_active else {}
        cmd = active_spec.get('command') or cat_spec.get('command') or ''
        args = active_spec.get('args') if active_spec.get('args') is not None else cat_spec.get('args', [])
        env = active_spec.get('env') or {}
        entry: Dict[str, Any] = {
            'type': 'local',
            'command': [cmd] + list(args),
            'disabled': not is_active,
        }
        if env:
            entry['environment'] = env
        servers_map[mcp_name] = entry

    # Append any custom servers passed in selected not present in default catalog
    if selected:
        for name, spec in selected.items():
            if name not in servers_map:
                cmd = spec.get('command') or ''
                args = spec.get('args') or []
                env = spec.get('env') or {}
                entry = {
                    'type': 'local',
                    'command': [cmd] + list(args),
                    'disabled': False,
                }
                if env:
                    entry['environment'] = env
                servers_map[name] = entry

    cfg_path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')

    # Clean up any legacy per-project opencode.json so global config takes precedence everywhere
    if project_dir and project_dir.is_dir():
        legacy_proj_cfg = project_dir / 'opencode.json'
        if legacy_proj_cfg.is_file() and legacy_proj_cfg.resolve() != cfg_path.resolve():
            try:
                legacy_proj_cfg.unlink()
            except Exception:
                pass
    return cfg_path


def launch_harness_by_id(harness_id: str, params: HarnessLaunch) -> Dict[str, Any]:
    """Auto-start backend server if needed, configure harness, then spawn CLI in terminal."""
    global harness_procs, opencode_proc, proc, log_handle

    if harness_id not in HARNESSES:
        raise HTTPException(400, f"Unknown harness '{harness_id}'. Choose one of: {', '.join(HARNESSES)}")

    project = Path(params.project_dir).expanduser().resolve()
    if not project.is_dir():
        raise HTTPException(400, f'Project directory does not exist: {project}')

    terminal = detect_terminal()
    if not terminal:
        raise HTTPException(400, 'No supported terminal emulator found')

    std_alias = get_standard_alias(params.backend)
    if not params.alias or params.alias in ('local-qwen', 'llamacpp', 'koboldcpp', 'ollama'):
        params.alias = std_alias

    with lock:
        health = server_health()
        if health is None and not alive():
            model_path = params.model_path or load_settings().get('last_model_path', '')
            if not model_path:
                raise HTTPException(400, 'No model selected and no last-used model found. Pick a model first.')
            path = safe_model_path(model_path)
            launch_backend_process(params.backend, path, params)

    save_settings({
        'last_model_path': params.model_path or load_settings().get('last_model_path', ''),
        'last_backend': params.backend,
        'last_alias': params.alias,
        'last_layers': params.layers,
        'last_context': params.context,
        'last_parallel': params.parallel,
        'last_threads': params.threads,
        'last_threads_batch': params.threads_batch,
        'last_batch': params.batch,
        'last_ubatch': params.ubatch,
        'last_flash_attn': params.flash_attn,
        'last_kv_cache': params.kv_cache,
        'last_context_extend': params.context_extend,
        'last_rag_enabled': params.rag_enabled,
        'last_temp': params.temp,
        'last_top_p': params.top_p,
        'last_top_k': params.top_k,
        'last_min_p': params.min_p,
        'last_repeat_penalty': params.repeat_penalty,
        'last_main_gpu': params.main_gpu,
        'last_split_mode': params.split_mode,
        'last_lan_access': params.lan_access,
        'last_reasoning': params.reasoning,
        'last_mtp_mode': params.mtp_mode,
        'last_spec_draft_n_max': params.spec_draft_n_max,
        'last_spec_draft_p_min': params.spec_draft_p_min,
        'last_mmproj_mode': params.mmproj_mode,
        'last_custom_flags': ' '.join(sanitize_managed_custom_flags(params.custom_flags.split())),
        'last_project_dir': str(project),
    })

    rag_file = None
    if params.rag_enabled:
        index_workspace(project)
        rag_file = RAG_CONTEXT_FILE

    cfg = sync_config()
    curr_api_key = get_current_api_key()
    warning_msg = None
    procs = harness_procs.setdefault(harness_id, [])

    if harness_id == 'opencode':
        write_opencode_config(project, params.alias, PORT, rag_context_file=rag_file, context_limit=params.context)
        oc_bin = cfg.get('agents', {}).get('opencode') or 'npx @opencode/cli@latest'
        cmd = (f'"{oc_bin}"' if ' ' in str(oc_bin) and not str(oc_bin).startswith('npx ') else f'{oc_bin}') + ' --auto'
        if params.restore_history:
            if params.session_id.strip():
                cmd += f' --session {params.session_id.strip()}'
            else:
                cmd += ' --continue'
        os.environ['OPENCODE_DISABLE_CLAUDE_CODE_SKILLS'] = '1'
        h_proc = spawn_in_terminal(cmd, cwd=project, title="OpenCode v2 CLI")
        opencode_proc = h_proc

    elif harness_id == 'claude':
        claude_bin = cfg.get('agents', {}).get('claude')
        if not claude_bin:
            raise HTTPException(400, 'Claude Code binary not found in venv or PATH. Run setup first.')
        base_url, ep_mode = resolve_base_url('anthropic', PORT, params.alias, curr_api_key)
        warning_msg = context_warning('claude', params.context)
        write_claude_settings(HOME, base_url, params.alias, context=params.context)
        write_claude_mcp_config(HOME)
        cmd = build_launch_command(
            'claude',
            claude_bin,
            params.alias,
            base_url,
            restore=params.restore_history,
            session_id=params.session_id,
            key_file=API_KEY_FILE,
            settings_file=HOME / '.claude' / 'llmcode.settings.json',
        )
        h_proc = spawn_in_terminal(cmd, cwd=project, title="Claude Code CLI")

    elif harness_id == 'codex':
        codex_bin = cfg.get('agents', {}).get('codex')
        if not codex_bin:
            raise HTTPException(400, 'Codex binary not found in venv or PATH. Run setup first.')
        base_url, ep_mode = resolve_base_url('responses', PORT, params.alias, curr_api_key)
        warning_msg = context_warning('codex', params.context)
        write_codex_profile(HOME, base_url, params.alias, context=params.context)
        cmd = build_launch_command(
            'codex',
            codex_bin,
            params.alias,
            base_url,
            restore=params.restore_history,
            session_id=params.session_id,
            key_file=API_KEY_FILE,
        )
        h_proc = spawn_in_terminal(cmd, cwd=project, title="Codex CLI")

    procs[:] = _prune_agent_procs(procs)
    procs.append({
        'pid': h_proc.pid,
        'proc': h_proc,
        'project_dir': str(project),
        'started_at': datetime.now().isoformat(timespec='seconds'),
    })

    label = HARNESSES[harness_id].label
    msg = f'{label} launching in terminal (model: {params.alias}).'
    if warning_msg:
        msg += f' Warning: {warning_msg}'

    return {
        'ok': True,
        'harness': harness_id,
        'message': msg,
        'warning': warning_msg,
        'terminal': terminal,
        'project_dir': str(project),
        'pid': h_proc.pid,
        'session_count': len(procs),
    }


@app.post('/api/harness/{harness_id}/launch')
def launch_harness_endpoint(harness_id: str, params: HarnessLaunch):
    params.harness = harness_id
    return launch_harness_by_id(harness_id, params)


@app.post('/api/opencode/launch')
def launch_opencode(params: OpenCodeLaunch):
    return launch_harness_by_id('opencode', params)


_sequence_state: Dict[str, Any] = {
    'running': False,
    'last_run': None,
}


def run_parallel_sequence(req: SequenceRunRequest) -> Dict[str, Any]:
    """Executes a fan-out / fan-in batch of prompts concurrently across active backend slots."""
    active_info = runtime_state.get('active') or {}
    st = load_settings()
    slot_budget = int(active_info.get('parallel') or st.get('last_parallel', 1) or 1)
    concurrency = max(1, min(32, int(req.max_concurrency) if req.max_concurrency > 0 else slot_budget))
    alias = active_info.get('alias') or st.get('last_alias', 'llamacpp') or 'llamacpp'
    default_temp = float(active_info.get('temp', st.get('last_temp', 0.2)))
    api_key = get_current_api_key()

    run_id = f"seq-{uuid.uuid4().hex[:8]}"
    started_ts = time.time()
    cancel_event = threading.Event()

    _sequence_state['running'] = True
    append_log(f"[SEQUENCE {run_id}] Starting {len(req.items)} tasks (concurrency={concurrency}, policy={req.join_policy})")

    def _run_item(idx: int, item: SequenceTaskItem) -> Dict[str, Any]:
        item_id = item.id.strip() or f"task-{idx + 1}"
        t0 = time.time()
        if cancel_event.is_set():
            return {
                'id': item_id,
                'index': idx,
                'status': 'cancelled',
                'content': '',
                'error': 'Cancelled by all_required fail-fast policy',
                'latency_ms': 0,
            }
        messages = []
        if item.system.strip():
            messages.append({'role': 'system', 'content': item.system.strip()})
        messages.append({'role': 'user', 'content': item.prompt})
        payload = {
            'model': alias,
            'messages': messages,
            'max_tokens': item.max_tokens,
            'temperature': item.temp if item.temp is not None else default_temp,
            'stream': False,
        }
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        url = f'http://127.0.0.1:{PORT}/v1/chat/completions'
        try:
            http_req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers=headers,
                method='POST',
            )
            with urllib.request.urlopen(http_req, timeout=req.timeout_sec) as resp:
                body = json.loads(resp.read(10_000_000))
            choices = body.get('choices') or []
            msg = (choices[0].get('message') or {}) if choices else {}
            content = msg.get('content') or (choices[0].get('text') if choices else '') or ''
            usage = body.get('usage') or {}
            return {
                'id': item_id,
                'index': idx,
                'status': 'ok',
                'content': content,
                'usage': usage,
                'error': None,
                'latency_ms': int((time.time() - t0) * 1000),
            }
        except Exception as e:
            if req.join_policy == 'all_required':
                cancel_event.set()
            return {
                'id': item_id,
                'index': idx,
                'status': 'error',
                'content': '',
                'error': str(e),
                'latency_ms': int((time.time() - t0) * 1000),
            }

    results: List[Optional[Dict[str, Any]]] = [None] * len(req.items)
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_map = {
            pool.submit(_run_item, idx, item): idx
            for idx, item in enumerate(req.items)
        }
        for fut in concurrent.futures.as_completed(future_map):
            idx = future_map[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {
                    'id': req.items[idx].id or f"task-{idx + 1}",
                    'index': idx,
                    'status': 'error',
                    'content': '',
                    'error': str(e),
                    'latency_ms': 0,
                }
            results[idx] = res
            if res['status'] == 'error' and req.join_policy == 'all_required':
                cancel_event.set()
                for pending_fut in future_map:
                    if not pending_fut.done():
                        pending_fut.cancel()

    final_items: List[Dict[str, Any]] = []
    ok_count = 0
    err_count = 0
    cancelled_count = 0
    for idx, r in enumerate(results):
        if r is None:
            r = {
                'id': req.items[idx].id or f"task-{idx + 1}",
                'index': idx,
                'status': 'cancelled',
                'content': '',
                'error': 'Cancelled before execution',
                'latency_ms': 0,
            }
        if r['status'] == 'ok':
            ok_count += 1
        elif r['status'] == 'cancelled':
            cancelled_count += 1
        else:
            err_count += 1
        final_items.append(r)

    if err_count == 0 and cancelled_count == 0:
        verdict = 'completed'
    elif req.join_policy == 'all_required':
        verdict = 'contained_failure'
    elif ok_count > 0:
        verdict = 'degraded'
    else:
        verdict = 'failed'

    total_ms = int((time.time() - started_ts) * 1000)
    summary = {
        'ok': verdict in ('completed', 'degraded'),
        'run_id': run_id,
        'verdict': verdict,
        'join_policy': req.join_policy,
        'concurrency': concurrency,
        'total_items': len(final_items),
        'succeeded': ok_count,
        'failed': err_count,
        'cancelled': cancelled_count,
        'total_latency_ms': total_ms,
        'completed_at': datetime.now().isoformat(timespec='seconds'),
        'results': final_items,
    }
    _sequence_state['running'] = False
    _sequence_state['last_run'] = summary
    append_log(f"[SEQUENCE {run_id}] Verdict={verdict} (ok={ok_count}, err={err_count}, cancelled={cancelled_count}, {total_ms}ms)")
    return summary


@app.post('/api/sequence/run')
def api_sequence_run(req: SequenceRunRequest):
    """Runs a batch of prompts in parallel across active server slots."""
    return run_parallel_sequence(req)


@app.get('/api/sequence/status')
def api_sequence_status():
    """Returns current parallel sequence execution status and most recent run summary."""
    active_info = runtime_state.get('active') or {}
    st = load_settings()
    return {
        'running': _sequence_state['running'],
        'parallel_slots': int(active_info.get('parallel') or st.get('last_parallel', 1) or 1),
        'effective_context': int(active_info.get('effective_context') or st.get('last_context', 8192)),
        'last_run': _sequence_state['last_run'],
    }
