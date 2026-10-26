import json, os, re, subprocess, threading, time, urllib.request, urllib.parse, urllib.error, shutil, signal, uuid
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import sys
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.paths import (
    STORE_DIR as STORE,
    MODEL_DIR,
    LOG_FILE,
    SETTINGS_FILE,
    DOWNLOADS_FILE,
    get_llama_server_binary,
    get_koboldcpp_binary,
    get_ollama_binary,
)
from src.common.process import detect_terminal, spawn_in_terminal, kill_process_tree

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
SERVER_BIN = get_llama_server_binary()
KOBOLD_BIN = get_koboldcpp_binary()
OLLAMA_BIN = get_ollama_binary()
PORT = int(os.environ.get('LLAMA_PORT','8080'))
lock = threading.RLock()
proc = None
log_handle = None
opencode_proc = None
aider_proc = None


def load_downloads():
    """Load persisted downloads state from disk."""
    if DOWNLOADS_FILE.exists():
        try:
            data = json.loads(DOWNLOADS_FILE.read_text())
            for d in data.values():
                if d.get('state') == 'downloading':
                    d['state'] = 'error'
                    d['error'] = 'Interrupted by server restart'
            return data
        except Exception:
            pass
    return {}


downloads = load_downloads()
settings = {'active': None, 'pid': None}
app = FastAPI(title='llmCode', docs_url='/api/docs', openapi_url='/api/openapi.json')


def save_downloads():
    """Persist downloads state to disk."""
    try:
        DOWNLOADS_FILE.write_text(json.dumps(downloads, indent=2) + '\n')
    except Exception:
        pass


def load_settings():
    """Load persisted settings (last-used model config, project directory)."""
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_settings(data):
    """Persist settings to disk."""
    current = load_settings()
    current.update(data)
    SETTINGS_FILE.write_text(json.dumps(current, indent=2) + '\n')


# detect_terminal imported from src.common.process


class Deploy(BaseModel):
    path: str
    backend: str = Field(default='llama.cpp', pattern=r'^(llama\.cpp|koboldcpp|ollama)$')
    alias: str = Field(pattern=r'^[a-zA-Z0-9_.-]{1,90}$')
    layers: int = Field(default=8, ge=0, le=100)
    context: int = Field(default=8192, ge=512, le=131072)
    threads: int = Field(default=4, ge=1, le=32)
    reasoning: bool = False
    flash_attn: str = Field(default='on', pattern='^(on|off)$')
    kv_cache: str = Field(default='q8_0', pattern='^(f16|q8_0|q4_0)$')
    temp: float = Field(default=0.2, ge=0.0, le=2.0)
    ubatch: int = Field(default=256, ge=32, le=2048)

class Download(BaseModel):
    repo: str
    filename: str

class OpenCodeLaunch(BaseModel):
    project_dir: str
    model_path: str = ''
    backend: str = Field(default='llama.cpp', pattern=r'^(llama\.cpp|koboldcpp|ollama)$')
    alias: str = Field(default='local-qwen', pattern=r'^[a-zA-Z0-9_.-]{1,90}$')
    layers: int = Field(default=8, ge=0, le=100)
    context: int = Field(default=8192, ge=512, le=131072)
    threads: int = Field(default=4, ge=1, le=32)
    flash_attn: str = Field(default='on', pattern='^(on|off)$')
    kv_cache: str = Field(default='q8_0', pattern='^(f16|q8_0|q4_0)$')
    temp: float = Field(default=0.2, ge=0.0, le=2.0)
    ubatch: int = Field(default=256, ge=32, le=2048)

class AiderLaunch(BaseModel):
    project_dir: str
    model_path: str = ''
    backend: str = Field(default='llama.cpp', pattern=r'^(llama\.cpp|koboldcpp|ollama)$')
    alias: str = Field(default='local-qwen', pattern=r'^[a-zA-Z0-9_.-]{1,90}$')
    layers: int = Field(default=8, ge=0, le=100)
    context: int = Field(default=8192, ge=512, le=131072)
    threads: int = Field(default=4, ge=1, le=32)
    flash_attn: str = Field(default='on', pattern='^(on|off)$')
    kv_cache: str = Field(default='q8_0', pattern='^(f16|q8_0|q4_0)$')
    temp: float = Field(default=0.2, ge=0.0, le=2.0)
    ubatch: int = Field(default=256, ge=32, le=2048)
    edit_format: str = Field(default='diff', pattern=r'^(diff|whole|udiff|architect)$')



def safe_model_path(path):
    p = Path(path).expanduser().resolve()
    if not p.is_relative_to(MODEL_DIR) or not p.is_file() or p.suffix.lower() != '.gguf':
        raise HTTPException(400, 'Choose an existing GGUF from the configured model directory')
    return p


def alive():
    global proc
    return bool(proc is not None and proc.poll() is None)


def http_json(url, timeout=8):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':'LocalLLM-Control-Center/0.1'}), timeout=timeout) as r:
        return json.loads(r.read(3_000_000))


def server_health():
    try:
        return http_json(f'http://127.0.0.1:{PORT}/health', 2)
    except Exception:
        pass
    try:
        return http_json(f'http://127.0.0.1:{PORT}/v1/models', 2)
    except Exception:
        return None


def system_status():
    gpu = {'available':False}
    try:
        r = subprocess.run(['nvidia-smi','--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu','--format=csv,noheader,nounits'], capture_output=True,text=True,timeout=4)
        if r.returncode == 0:
            v=[x.strip() for x in r.stdout.splitlines()[0].split(',')]
            if len(v)>=5:
                gpu = {'available':True,'name':v[0],'total_mb':int(v[1]),'used_mb':int(v[2]),'util_pct':int(v[3]),'temperature_c':int(v[4])}
    except Exception: pass
    mem={}
    try:
        rows={}
        for line in Path('/proc/meminfo').read_text().splitlines():
            k,_,v=line.partition(':'); rows[k]=int(v.strip().split()[0])
        mem={'total_mb':rows['MemTotal']//1024,'available_mb':rows['MemAvailable']//1024}
    except Exception: pass
    return {'gpu':gpu,'ram':mem,'binary':str(SERVER_BIN),'binary_exists':SERVER_BIN.is_file(),'model_dir':str(MODEL_DIR),'model_dir_exists':MODEL_DIR.is_dir(),'server_port':PORT}

@app.get('/',response_class=HTMLResponse)
def index():
    return (ROOT/'static'/'index.html').read_text()

@app.get('/api/status')
def status():
    with lock:
        running=alive()
        return {'managed_running':running,'managed_pid':proc.pid if running else None,'active':settings.get('active') if running else None,'health':server_health(),**system_status()}

@app.get('/api/models')
def models():
    found=[]
    if MODEL_DIR.exists():
        for p in sorted(MODEL_DIR.rglob('*.gguf')):
            if p.is_file():
                st=p.stat(); found.append({'path':str(p),'name':p.name,'relative':str(p.relative_to(MODEL_DIR)),'size_gb':round(st.st_size/1e9,2),'size_bytes':st.st_size,'modified':datetime.fromtimestamp(st.st_mtime).isoformat(timespec='minutes'),'mmproj':'mmproj' in p.name.lower()})
            if len(found)>=500: break
    return {'models':found,'directory':str(MODEL_DIR)}


def launch_backend_process(backend, path, params):
    """Launch llama.cpp, koboldcpp, or ollama on port PORT."""
    global proc, log_handle
    if backend == 'koboldcpp':
        if not KOBOLD_BIN.is_file():
            raise HTTPException(400, f'koboldcpp not found at {KOBOLD_BIN}')
        args = ['python3', str(KOBOLD_BIN), '--model', str(path),
                '--port', str(PORT), '--contextsize', str(params.context),
                '--gpulayers', str(params.layers), '--threads', str(params.threads),
                '--usecuda', '--smartcontext', '--skiplauncher', '--quiet']
    elif backend == 'ollama':
        if not OLLAMA_BIN.is_file():
            raise HTTPException(400, f'ollama binary not found at {OLLAMA_BIN}')
        # Create or update an Ollama modelfile pointing to this GGUF
        model_name = params.alias or 'local-gguf'
        modelfile_path = STORE / 'Modelfile'
        modelfile_path.write_text(f"FROM {path}\nPARAMETER num_ctx {params.context}\nPARAMETER num_gpu {params.layers}\n")
        # Ensure ollama serve runs
        env = os.environ.copy()
        env['OLLAMA_HOST'] = f'127.0.0.1:{PORT}'
        args = [str(OLLAMA_BIN), 'serve']
    else:  # default llama.cpp
        if not SERVER_BIN.is_file():
            raise HTTPException(400, f'llama-server not found at {SERVER_BIN}')
        args = [str(SERVER_BIN), '--model', str(path), '--alias', params.alias,
                '--host', '127.0.0.1', '--port', str(PORT),
                '--ctx-size', str(params.context), '--n-gpu-layers', str(params.layers),
                '--threads', str(params.threads), '--parallel', '1', '--jinja',
                '-fa', params.flash_attn, '--temp', str(params.temp), '-ub', str(params.ubatch)]
        if params.kv_cache != 'f16':
            args += ['-ctk', params.kv_cache, '-ctv', params.kv_cache]
        if hasattr(params, 'reasoning') and not params.reasoning:
            args += ['--reasoning', 'off']

    log_handle = open(LOG_FILE, 'a', buffering=1)
    log_handle.write(f'\n===== [{backend}] ' + datetime.now().isoformat() + ' =====\n')
    env = os.environ.copy()
    if backend == 'ollama':
        env['OLLAMA_HOST'] = f'127.0.0.1:{PORT}'
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log_handle,
                            stderr=subprocess.STDOUT, start_new_session=True, cwd=str(STORE), env=env)
    settings['active'] = {
        'backend': backend,
        'name': path.name,
        'path': str(path),
        'alias': params.alias,
        'layers': params.layers,
        'context': params.context,
        'threads': params.threads,
        'flash_attn': params.flash_attn,
        'kv_cache': params.kv_cache,
        'temp': params.temp,
        'ubatch': params.ubatch
    }
    settings['pid'] = proc.pid
    return args

@app.post('/api/start')
def start(params:Deploy):
    path=safe_model_path(params.path)
    with lock:
        if alive(): raise HTTPException(409,'A server started by this dashboard is already running. Stop it first.')
        args = launch_backend_process(params.backend, path, params)
        return {'ok':True,'pid':proc.pid,'command':args,'message':f'[{params.backend}] launching; health may take time to become ready.'}

@app.post('/api/stop')
def stop():
    global proc
    with lock:
        if not alive(): return {'ok':True,'message':'No dashboard-managed server running'}
        pid = proc.pid
        kill_process_tree(pid)
        try: proc.wait(timeout=3)
        except Exception: pass
        return {'ok':True,'message':f'Stopped dashboard-managed process {pid}'}

@app.get('/api/logs')
def logs():
    if not LOG_FILE.exists(): return {'text':'No server log yet.'}
    with open(LOG_FILE,'rb') as f:
        f.seek(0,2); f.seek(max(0,f.tell()-24000)); data=f.read()
    return {'text':data.decode('utf-8','replace')[-22000:]}

@app.get('/api/hf/search')
def hf_search(q:str='Qwen3.5',limit:int=20):
    limit=max(1,min(limit,50))
    query=urllib.parse.urlencode({'search':q,'limit':limit,'sort':'downloads','direction':-1,'filter':'gguf'})
    try:
        result=http_json('https://huggingface.co/api/models?'+query,timeout=14)
        return {'models':[{'id':r.get('id'),'downloads':r.get('downloads',0),'likes':r.get('likes',0),'private':r.get('private',False),'gated':r.get('gated',False)} for r in result]}
    except Exception as e: raise HTTPException(502,f'Hugging Face query failed: {e}')

@app.get('/api/hf/files')
def hf_files(repo:str):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+',repo): raise HTTPException(400,'Invalid repo id')
    try:
        result=http_json('https://huggingface.co/api/models/'+repo+'/tree/main?recursive=true&expand=false',timeout=20)
        files=[{'filename':f.get('path'),'size_gb':round((f.get('size') or 0)/1e9,2),'size_bytes':f.get('size') or 0} for f in result if f.get('path','').lower().endswith('.gguf')]
        return {'files':files}
    except Exception as e: raise HTTPException(502,f'Hugging Face file listing failed: {e}')


def download_worker(did,repo,filename):
    entry=downloads[did]
    try:
        MODEL_DIR.mkdir(parents=True,exist_ok=True)
        dst_dir=(MODEL_DIR/repo.replace('/','__')).resolve()
        if not dst_dir.is_relative_to(MODEL_DIR): raise ValueError('Bad destination')
        dst_dir.mkdir(parents=True,exist_ok=True)
        out=dst_dir/filename
        part=dst_dir/(filename+'.part')
        if out.exists(): raise ValueError('File already exists; will not overwrite')
        entry['path']=str(out)
        link='https://huggingface.co/'+repo+'/resolve/main/'+urllib.parse.quote(filename,safe='/')
        req=urllib.request.Request(link,headers={'User-Agent':'LocalLLM-Control-Center/0.1'})
        with urllib.request.urlopen(req,timeout=60) as resp, open(part,'wb') as w:
            entry['total']=int(resp.headers.get('Content-Length',0))
            save_downloads()
            last_save = time.time()
            while True:
                chunk=resp.read(1024*1024)
                if not chunk:break
                w.write(chunk);entry['received']+=len(chunk)
                if time.time() - last_save >= 1.0:
                    save_downloads()
                    last_save = time.time()
        if entry['received']<1024: raise ValueError('Unexpectedly small GGUF download')
        os.replace(part,out)
        entry['state']='complete'
        save_downloads()
    except Exception as e:
        entry['state']='error';entry['error']=str(e)
        save_downloads()

@app.post('/api/hf/download')
def hf_download(params:Download):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+',params.repo): raise HTTPException(400,'Invalid repository')
    if not params.filename.lower().endswith('.gguf') or params.filename.startswith('/') or '..' in Path(params.filename).parts or '\\' in params.filename: raise HTTPException(400,'Invalid GGUF filename')
    # Fetch repo inventory to ensure download is selected from actual repository.
    result=hf_files(params.repo)
    if params.filename not in [f['filename'] for f in result['files']]: raise HTTPException(400,'GGUF is not listed in repository')
    did=uuid.uuid4().hex[:12]
    downloads[did]={'id':did,'repo':params.repo,'filename':params.filename,'received':0,'total':0,'state':'downloading','error':None}
    save_downloads()
    threading.Thread(target=download_worker,args=(did,params.repo,params.filename),daemon=True).start()
    return downloads[did]

@app.get('/api/downloads')
def get_downloads():return {'downloads':list(downloads.values())}

@app.get('/api/settings')
def get_settings():
    return load_settings()


@app.get('/api/directories')
def list_directories(base: str = ''):
    """List subdirectories for project directory picker."""
    target = Path(base or str(HOME)).expanduser().resolve()
    if not target.is_dir():
        raise HTTPException(400, 'Not a directory')
    dirs = []
    try:
        for entry in sorted(target.iterdir()):
            if entry.is_dir() and not entry.name.startswith('.'):
                dirs.append({'name': entry.name, 'path': str(entry)})
    except PermissionError:
        pass
    return {'parent': str(target.parent), 'current': str(target), 'directories': dirs[:200]}


@app.get('/api/opencode/status')
def opencode_status():
    global opencode_proc
    running = opencode_proc is not None and opencode_proc.poll() is None
    opencode_home_bin = Path.home() / '.opencode' / 'bin' / 'opencode'
    found_bin = str(opencode_home_bin) if opencode_home_bin.is_file() else (shutil.which('opencode') or shutil.which('opencode2'))
    return {
        'running': running,
        'pid': opencode_proc.pid if running else None,
        'installed': found_bin is not None,
        'binary': found_bin,
        'terminal': detect_terminal(),
    }


@app.post('/api/opencode/launch')
def launch_opencode(params: OpenCodeLaunch):
    """Auto-start llama-server if needed, then spawn opencode CLI in a terminal."""
    global opencode_proc, proc, log_handle


    project = Path(params.project_dir).expanduser().resolve()
    if not project.is_dir():
        raise HTTPException(400, f'Project directory does not exist: {project}')

    terminal = detect_terminal()
    if not terminal:
        raise HTTPException(400, 'No supported terminal emulator found')

    # Auto-start llama-server if not running
    with lock:
        health = server_health()
        if health is None and not alive():
            model_path = params.model_path
            if not model_path:
                saved = load_settings()
                model_path = saved.get('last_model_path', '')
            if not model_path:
                raise HTTPException(400, 'No model selected and no last-used model found. Pick a model first.')

            path = safe_model_path(model_path)
            if not SERVER_BIN.is_file():
                raise HTTPException(400, f'llama-server not found at {SERVER_BIN}')

            args = [str(SERVER_BIN), '--model', str(path), '--alias', params.alias,
                    '--host', '127.0.0.1', '--port', str(PORT),
                    '--ctx-size', str(params.context), '--n-gpu-layers', str(params.layers),
                    '--threads', str(params.threads), '--parallel', '1', '--jinja',
                    '-fa', params.flash_attn, '--temp', str(params.temp), '-ub', str(params.ubatch)]
            if params.kv_cache != 'f16':
                args += ['-ctk', params.kv_cache, '-ctv', params.kv_cache]
            log_handle = open(LOG_FILE, 'a', buffering=1)
            log_handle.write('\n===== ' + datetime.now().isoformat() + ' =====\n')
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log_handle,
                                    stderr=subprocess.STDOUT, start_new_session=True, cwd=str(STORE))
            settings['active'] = {'name': path.name, 'path': str(path), 'alias': params.alias,
                                  'layers': params.layers, 'context': params.context, 'threads': params.threads,
                                  'flash_attn': params.flash_attn, 'kv_cache': params.kv_cache, 'temp': params.temp, 'ubatch': params.ubatch}
            settings['pid'] = proc.pid

    save_settings({
        'last_model_path': params.model_path or load_settings().get('last_model_path', ''),
        'last_alias': params.alias,
        'last_layers': params.layers,
        'last_context': params.context,
        'last_threads': params.threads,
        'last_flash_attn': params.flash_attn,
        'last_kv_cache': params.kv_cache,
        'last_temp': params.temp,
        'last_ubatch': params.ubatch,
        'last_project_dir': str(project),
    })

    # Command: run opencode standalone directly in terminal
    opencode_home_bin = Path.home() / '.opencode' / 'bin' / 'opencode'
    if opencode_home_bin.is_file():
        oc_bin = str(opencode_home_bin)
    else:
        oc_bin = shutil.which('opencode') or shutil.which('opencode2') or 'npx opencode-ai'
    oc_cmd = f'{oc_bin} --standalone'

    opencode_proc = spawn_in_terminal(oc_cmd, cwd=project, title="OpenCode CLI")
    return {
        'ok': True,
        'message': 'OpenCode CLI launching in terminal. It will wait for llama-server to be ready.',
        'terminal': terminal,
        'project_dir': str(project),
        'opencode_pid': opencode_proc.pid,
        'server_was_running': health is not None or (alive() and params.model_path == ''),
    }


@app.get('/api/aider/status')
def aider_status():
    global aider_proc
    running = aider_proc is not None and aider_proc.poll() is None
    venv_bin = ROOT / '.venv' / 'bin' / 'aider'
    found_bin = str(venv_bin) if venv_bin.is_file() else (shutil.which('aider') or str(Path.home() / '.local' / 'bin' / 'aider'))
    installed = Path(found_bin).is_file() or shutil.which('aider') is not None
    return {
        'running': running,
        'pid': aider_proc.pid if running else None,
        'installed': installed,
        'binary': found_bin if installed else None,
        'terminal': detect_terminal(),
    }


@app.post('/api/aider/launch')
def launch_aider(params: AiderLaunch):
    """Auto-start llama-server if needed, then spawn aider in a terminal."""
    global aider_proc, proc, log_handle

    project = Path(params.project_dir).expanduser().resolve()
    if not project.is_dir():
        raise HTTPException(400, f'Project directory does not exist: {project}')

    terminal = detect_terminal()
    if not terminal:
        raise HTTPException(400, 'No supported terminal emulator found')

    # Locate aider binary
    venv_bin = ROOT / '.venv' / 'bin' / 'aider'
    if venv_bin.is_file():
        aider_bin = str(venv_bin)
    else:
        aider_bin = shutil.which('aider') or str(Path.home() / '.local' / 'bin' / 'aider')
    if not Path(aider_bin).is_file() and not shutil.which('aider'):
        raise HTTPException(400, 'Aider CLI binary not found in venv or PATH')

    # Auto-start llama-server if not running
    with lock:
        health = server_health()
        if health is None and not alive():
            model_path = params.model_path
            if not model_path:
                saved = load_settings()
                model_path = saved.get('last_model_path', '')
            if not model_path:
                raise HTTPException(400, 'No model selected and no last-used model found. Pick a model first.')

            path = safe_model_path(model_path)
            if not SERVER_BIN.is_file():
                raise HTTPException(400, f'llama-server not found at {SERVER_BIN}')

            args = [str(SERVER_BIN), '--model', str(path), '--alias', params.alias,
                    '--host', '127.0.0.1', '--port', str(PORT),
                    '--ctx-size', str(params.context), '--n-gpu-layers', str(params.layers),
                    '--threads', str(params.threads), '--parallel', '1', '--jinja',
                    '-fa', params.flash_attn, '--temp', str(params.temp), '-ub', str(params.ubatch)]
            if params.kv_cache != 'f16':
                args += ['-ctk', params.kv_cache, '-ctv', params.kv_cache]
            log_handle = open(LOG_FILE, 'a', buffering=1)
            log_handle.write('\n===== ' + datetime.now().isoformat() + ' =====\n')
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log_handle,
                                    stderr=subprocess.STDOUT, start_new_session=True, cwd=str(STORE))
            settings['active'] = {'name': path.name, 'path': str(path), 'alias': params.alias,
                                  'layers': params.layers, 'context': params.context, 'threads': params.threads,
                                  'flash_attn': params.flash_attn, 'kv_cache': params.kv_cache, 'temp': params.temp, 'ubatch': params.ubatch}
            settings['pid'] = proc.pid

    save_settings({
        'last_model_path': params.model_path or load_settings().get('last_model_path', ''),
        'last_alias': params.alias,
        'last_layers': params.layers,
        'last_context': params.context,
        'last_threads': params.threads,
        'last_flash_attn': params.flash_attn,
        'last_kv_cache': params.kv_cache,
        'last_temp': params.temp,
        'last_ubatch': params.ubatch,
        'last_project_dir': str(project),
        'last_edit_format': params.edit_format,
    })

    # Command: run aider connected to local llama-server
    model_arg = f'openai/{params.alias}'
    edit_arg = f'--edit-format {params.edit_format}'
    aider_cmd = f'{aider_bin} --openai-api-base http://127.0.0.1:{PORT}/v1 --openai-api-key none --model {model_arg} {edit_arg} --stream --dark-mode'

    aider_proc = spawn_in_terminal(aider_cmd, cwd=project, title="Aider CLI")

    return {
        'ok': True,
        'message': 'Aider CLI launching in terminal. Connecting to local llama-server.',
        'terminal': terminal,
        'project_dir': str(project),
        'aider_pid': aider_proc.pid,
        'server_was_running': health is not None or (alive() and params.model_path == ''),
    }



