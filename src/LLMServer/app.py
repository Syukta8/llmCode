import json, os, re, secrets, subprocess, threading, time, urllib.request, urllib.parse, urllib.error, shutil, signal, uuid
from pathlib import Path
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

import sys
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.paths import (
    CONFIG_DIR as STORE,
    CONFIG_FILE,
    DAEMON_LOG_FILE,
    HARDWARE_FILE,
    MODEL_DIR,
    LOG_FILE,
    get_llama_server_binary,
    get_koboldcpp_binary,
    get_ollama_binary,
    get_standard_alias,
    load_config,
    load_hardware,
    save_config,
    sync_config,
)
from src.common.process import detect_terminal, spawn_in_terminal, kill_process_tree
from src.common.rag import (
    RAG_CONTEXT_FILE,
    RAG_INDEX_FILE,
    generate_rag_context_file,
    index_workspace,
    retrieve_chunks,
)

# Auto-verify and sync paths in config/config.json on server startup
_initial_cfg = sync_config()

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get('LLAMA_PORT', str(_initial_cfg.get('settings', {}).get('server_port', 8080))))
lock = threading.RLock()
proc = None
log_handle = None
opencode_proc = None
aider_proc = None


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
    """Load persisted settings from config/config.json."""
    return load_config().get('settings', {})

def get_or_create_api_key() -> str:
    """Retrieves or auto-generates a secure random API key persisted in config/config.json."""
    settings = load_settings()
    key = settings.get('api_key') or os.environ.get('LLM_API_KEY')
    if not key:
        key = f"sk-{secrets.token_urlsafe(24)}"
        save_settings({'api_key': key})
    return key



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
    threads: int = Field(default=4, ge=1, le=256)
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


class OpenCodeLaunch(BaseModel):
    project_dir: str
    model_path: str = ''
    backend: str = Field(default='llama.cpp', pattern=r'^(llama\.cpp|koboldcpp|ollama)$')
    alias: str = Field(default='llamacpp', pattern=r'^[a-zA-Z0-9_.-]{1,90}$')
    layers: int = Field(default=8, ge=0, le=999)
    context: int = Field(default=8192, ge=512, le=1048576)
    threads: int = Field(default=4, ge=1, le=256)
    batch: int = Field(default=2048, ge=32, le=65536)
    ubatch: int = Field(default=256, ge=32, le=16384)
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
    custom_flags: str = ''
    restore_history: bool = False
    session_id: str = ''


class AiderLaunch(BaseModel):
    project_dir: str
    model_path: str = ''
    backend: str = Field(default='llama.cpp', pattern=r'^(llama\.cpp|koboldcpp|ollama)$')
    alias: str = Field(default='llamacpp', pattern=r'^[a-zA-Z0-9_.-]{1,90}$')
    layers: int = Field(default=8, ge=0, le=999)
    context: int = Field(default=8192, ge=512, le=1048576)
    threads: int = Field(default=4, ge=1, le=256)
    batch: int = Field(default=2048, ge=32, le=65536)
    ubatch: int = Field(default=256, ge=32, le=16384)
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
    custom_flags: str = ''
    edit_format: str = Field(default='diff', pattern=r'^(diff|whole|udiff|architect)$')
    restore_history: bool = False
    history_file: str = ''


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
    headers = {'Authorization': f'Bearer {get_or_create_api_key()}'}
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
    gpu = {'available': False}
    try:
        if sys.platform == 'darwin':
            res = subprocess.run(['system_profiler', 'SPDisplaysDataType'], capture_output=True, text=True, timeout=4)
            if res.returncode == 0:
                m = re.search(r'Chipset Model:\s*(.+)', res.stdout)
                name = m.group(1).strip() if m else 'Apple Metal GPU'
                gpu = {'available': True, 'name': name, 'total_mb': 0, 'used_mb': 0, 'util_pct': 0, 'temperature_c': 0}
        else:
            r = subprocess.run(
                ['nvidia-smi', '--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=4
            )
            if r.returncode == 0:
                v = [x.strip() for x in r.stdout.splitlines()[0].split(',')]
                if len(v) >= 5:
                    gpu = {'available': True, 'name': v[0], 'total_mb': int(v[1]), 'used_mb': int(v[2]), 'util_pct': int(v[3]), 'temperature_c': int(v[4])}
    except Exception:
        pass

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
    return {
        'gpu': gpu,
        'ram': mem,
        'binary': str(server_bin),
        'binary_exists': server_bin.is_file(),
        'model_dir': str(MODEL_DIR),
        'model_dir_exists': MODEL_DIR.is_dir(),
        'config_file': str(CONFIG_FILE),
        'server_port': PORT,
        'api_key': get_or_create_api_key(),
        'platform': sys.platform,
    }


@app.get('/', response_class=HTMLResponse)
def index():
    return (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')


@app.get('/api/status')
def status():
    with lock:
        running = alive()
        return {
            'managed_running': running,
            'managed_pid': proc.pid if running else None,
            'active': runtime_state.get('active') if running else None,
            'health': server_health(),
            **system_status(),
        }


@app.get('/api/hardware')
def hardware():
    """Returns scanned hardware profile and build/runtime recommendations from config/hardware.json."""
    return load_hardware()


@app.get('/api/models')
def models():
    found = []
    if MODEL_DIR.exists():
        for p in sorted(MODEL_DIR.rglob('*.gguf')):
            if p.is_file():
                st = p.stat()
                found.append({
                    'path': str(p),
                    'name': p.name,
                    'relative': p.relative_to(MODEL_DIR).as_posix(),
                    'size_gb': round(st.st_size / 1e9, 2),
                    'size_bytes': st.st_size,
                    'modified': datetime.fromtimestamp(st.st_mtime).isoformat(timespec='minutes'),
                    'mmproj': 'mmproj' in p.name.lower(),
                })
            if len(found) >= 500:
                break
    return {'models': found, 'directory': str(MODEL_DIR)}


def build_context_extension_flags(backend: str, preset: str) -> list:
    """
    Translates Context Extension preset ('none', 'linear_2x', 'yarn_2x', 'yarn_4x', 'yarn_8x')
    into backend-specific RoPE / YaRN CLI flags.
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
        return flags
    return []


def launch_backend_process(backend, path, params):
    """Launch llama.cpp, koboldcpp, or ollama on port PORT with standard alias and extended parameter support."""
    global proc, log_handle
    sync_config()
    server_bin = get_llama_server_binary()
    kobold_bin = get_koboldcpp_binary()
    ollama_bin = get_ollama_binary()

    # Standardize alias to llamacpp / koboldcpp / ollama unless user explicitly customized it
    std_alias = get_standard_alias(backend)
    if not getattr(params, 'alias', '') or params.alias in ('local-qwen', 'llamacpp', 'koboldcpp', 'ollama'):
        params.alias = std_alias

    ctx_ext = getattr(params, 'context_extend', 'none') or 'none'
    rag_on = bool(getattr(params, 'rag_enabled', False))
    ext_flags = build_context_extension_flags(backend, ctx_ext)
    custom_args = [arg for arg in params.custom_flags.split() if arg.strip()] if getattr(params, 'custom_flags', None) else []

    if backend == 'koboldcpp':
        if not kobold_bin.is_file():
            raise HTTPException(400, f'koboldcpp not found at {kobold_bin}')
        base_cmd = [sys.executable, str(kobold_bin)] if kobold_bin.suffix.lower() == '.py' else [str(kobold_bin)]
        args = base_cmd + [
            '--model', str(path),
            '--port', str(PORT),
            '--contextsize', str(params.context),
            '--gpulayers', str(params.layers),
            '--threads', str(params.threads),
            '--blasbatchsize', str(params.batch),
            '--usemmap',
            '--skiplauncher', '--quiet', '--host', '0.0.0.0',
        ]
        if params.layers > 0 and sys.platform != 'darwin':
            args.append('--usecuda')
        if params.flash_attn == 'on':
            args.append('--flashattention')
        if params.kv_cache == 'q8_0':
            args.extend(['--quantkv', '1'])
        elif params.kv_cache == 'q4_0':
            args.extend(['--quantkv', '2'])
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
            f"PARAMETER num_ctx {params.context}",
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
        args = [
            str(server_bin), '--model', str(path), '--alias', params.alias,
            '--host', '0.0.0.0', '--port', str(PORT),
            '--api-key', get_or_create_api_key(),
            '--ctx-size', str(params.context),
            '--n-gpu-layers', str(params.layers),
            '--threads', str(params.threads),
            '-b', str(params.batch),
            '-ub', str(params.ubatch),
            '--temp', str(params.temp),
            '--top-p', str(params.top_p),
            '--top-k', str(params.top_k),
            '--min-p', str(params.min_p),
            '--repeat-penalty', str(params.repeat_penalty),
            '--parallel', '1', '--jinja',
            '-fa', params.flash_attn,
            '-sm', params.split_mode,
            '--main-gpu', str(params.main_gpu),
        ]
        if params.kv_cache != 'f16':
            args += ['-ctk', params.kv_cache, '-ctv', params.kv_cache]
        if hasattr(params, 'reasoning') and not params.reasoning:
            args += ['--reasoning', 'off']
        else:
            args += ['--reasoning', 'on']
        if rag_on:
            args += ['--embeddings', '--pooling', 'mean']
        args.extend(ext_flags)
        args.extend(custom_args)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(LOG_FILE, 'a', buffering=1, encoding='utf-8')
    cmd_str = ' '.join(f'"{a}"' if ' ' in a else a for a in args)
    log_handle.write(f'\n===== [{backend}] {datetime.now().isoformat(timespec="seconds")} =====\n')
    log_handle.write(f'[COMMAND] {cmd_str}\n')
    log_handle.flush()

    env = os.environ.copy()
    if backend == 'ollama':
        env['OLLAMA_HOST'] = f'0.0.0.0:{PORT}'

    # Ensure the binary's directory is in PATH so Windows finds companion CUDA DLLs (ggml-cuda.dll, cublas64_*.dll)
    bin_parent = str(Path(args[0]).resolve().parent)
    env['PATH'] = bin_parent + os.pathsep + env.get('PATH', '')

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
        'context': params.context,
        'threads': params.threads,
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
        'custom_flags': params.custom_flags,
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
            'last_threads': params.threads,
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
            'last_custom_flags': params.custom_flags,
        })
        return {'ok': True, 'pid': proc.pid, 'command': args, 'message': f'[{params.backend}] launching; health may take time to become ready.'}


@app.post('/api/stop')
def stop():
    global proc
    with lock:
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


@app.get('/api/opencode/status')
def opencode_status():
    global opencode_proc
    running = opencode_proc is not None and opencode_proc.poll() is None
    cfg = sync_config()
    found_bin = cfg.get('agents', {}).get('opencode')
    installed = bool(found_bin)
    return {
        'running': running,
        'pid': opencode_proc.pid if running else None,
        'installed': installed,
        'binary': found_bin,
        'terminal': detect_terminal(),
    }


def write_opencode_config(project_dir: Path, alias: str, port: int, rag_context_file: Optional[Path] = None) -> Path:
    """
    Writes or updates opencode.json in the target project directory so OpenCode CLI
    connects directly to the local backend API using the standard alias,
    enables build agent, full edit/bash tools, and sets reasoning effort to high.
    """
    cfg_path = project_dir / 'opencode.json'
    data = {}
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding='utf-8'))
        except Exception:
            data = {}
    data.setdefault('$schema', 'https://opencode.ai/config.json')
    providers = data.setdefault('provider', {})
    providers['llmcode'] = {
        'npm': '@ai-sdk/openai-compatible',
        'name': f'llmCode ({alias})',
        'options': {
            'baseURL': f'http://127.0.0.1:{port}/v1',
            'apiKey': 'EMPTY',
        },
        'models': {
            alias: {
                'name': alias,
                'reasoning': True,
                'reasoning_options': [
                    {'type': 'effort', 'values': ['high']}
                ]
            }
        }
    }
    data['model'] = f'llmcode/{alias}'
    data['default_agent'] = 'build'
    data['permission'] = {
        'read': 'allow',
        'edit': 'allow',
        'glob': 'allow',
        'grep': 'allow',
        'list': 'allow',
        'bash': 'allow',
        'task': 'allow',
        'todowrite': 'allow',
        'question': 'allow'
    }
    if rag_context_file and rag_context_file.is_file():
        data['instructions'] = [str(rag_context_file.resolve())]
    cfg_path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    return cfg_path


@app.post('/api/opencode/launch')
def launch_opencode(params: OpenCodeLaunch):
    """Auto-start backend server if needed, configure opencode.json with standard alias, then spawn opencode CLI."""
    global opencode_proc, proc, log_handle

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
        'last_threads': params.threads,
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
        'last_custom_flags': params.custom_flags,
        'last_project_dir': str(project),
    })

    rag_file = None
    if params.rag_enabled:
        index_workspace(project)
        rag_file = RAG_CONTEXT_FILE

    write_opencode_config(project, params.alias, PORT, rag_context_file=rag_file)

    cfg = sync_config()
    oc_bin = cfg.get('agents', {}).get('opencode') or 'npx opencode-ai'
    oc_cmd = (f'"{oc_bin}"' if ' ' in str(oc_bin) and not str(oc_bin).startswith('npx ') else f'{oc_bin}') + ' --auto'
    if params.restore_history:
        if params.session_id.strip():
            oc_cmd += f' --session {params.session_id.strip()}'
        else:
            oc_cmd += ' --continue'
    opencode_proc = spawn_in_terminal(oc_cmd, cwd=project, title="OpenCode CLI")
    return {
        'ok': True,
        'message': f'OpenCode CLI launching in terminal (model: llmcode/{params.alias}).',
        'terminal': terminal,
        'project_dir': str(project),
        'opencode_pid': opencode_proc.pid,
    }


@app.get('/api/aider/status')
def aider_status():
    global aider_proc
    running = aider_proc is not None and aider_proc.poll() is None
    cfg = sync_config()
    found_bin = cfg.get('agents', {}).get('aider')
    installed = bool(found_bin)
    return {
        'running': running,
        'pid': aider_proc.pid if running else None,
        'installed': installed,
        'binary': found_bin if installed else None,
        'terminal': detect_terminal(),
    }


@app.post('/api/aider/launch')
def launch_aider(params: AiderLaunch):
    """Auto-start backend server if needed, then spawn aider in a terminal."""
    global aider_proc, proc, log_handle

    project = Path(params.project_dir).expanduser().resolve()
    if not project.is_dir():
        raise HTTPException(400, f'Project directory does not exist: {project}')

    terminal = detect_terminal()
    if not terminal:
        raise HTTPException(400, 'No supported terminal emulator found')

    cfg = sync_config()
    aider_bin = cfg.get('agents', {}).get('aider')
    if not aider_bin:
        raise HTTPException(400, 'Aider CLI binary not found in venv or PATH. Run setup first.')

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
        'last_threads': params.threads,
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
        'last_custom_flags': params.custom_flags,
        'last_project_dir': str(project),
        'last_edit_format': params.edit_format,
    })

    model_arg = f'openai/{params.alias}'
    edit_arg = f'--edit-format {params.edit_format}'
    aider_cmd = f'"{aider_bin}" --openai-api-base http://127.0.0.1:{PORT}/v1 --openai-api-key none --model {model_arg} {edit_arg} --stream --dark-mode --auto-accept-architect --yes-always --auto-commits --reasoning-effort high'
    if params.rag_enabled:
        index_workspace(project)
        aider_cmd += f' --read "{RAG_CONTEXT_FILE}"'
    if params.restore_history:
        aider_cmd += ' --restore-chat-history'
    if params.history_file.strip():
        aider_cmd += f' --chat-history-file {params.history_file.strip()}'

    aider_proc = spawn_in_terminal(aider_cmd, cwd=project, title="Aider CLI")
    return {
        'ok': True,
        'message': 'Aider CLI launching in terminal. Connecting to local server.',
        'terminal': terminal,
        'project_dir': str(project),
        'aider_pid': aider_proc.pid,
    }
