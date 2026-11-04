#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llmCode - Unified Cross-Platform Local LLM CLI Runner
Provides a single interface for setup, downloading, serving, dashboard
daemon lifecycle, and stopping services across Windows, Linux, and macOS.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.paths import (
    AGENT_ENV_FILE,
    API_KEY_FILE,
    CONFIG_FILE,
    DAEMON_LOG_FILE,
    DEPLOY_STATE_FILE,
    LOG_FILE,
    MODEL_DIR,
    PID_FILE,
    build_binary_env,
    get_koboldcpp_binary,
    get_llama_server_binary,
    get_ollama_binary,
    sync_config,
)
from src.common.process import (
    is_pid_alive,
    spawn_in_terminal,
    stop_all_services,
)


def sanitize_system_path() -> None:
    """
    Filters out inaccessible, non-existent, or untrusted mount/junction points
    from os.environ['PATH'] to prevent OSError [WinError 448] on Windows.
    """
    raw_path = os.environ.get("PATH", "")
    if not raw_path:
        return
    valid_parts = []
    for part in raw_path.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        try:
            p = Path(part)
            if p.is_dir():
                valid_parts.append(part)
        except OSError:
            continue
    os.environ["PATH"] = os.pathsep.join(valid_parts)


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


def _is_dash_healthy(port: int) -> bool:
    """Checks if llm-dash HTTP endpoint responds on 127.0.0.1:port."""
    try:
        url = f"http://127.0.0.1:{port}/api/status"
        req = urllib.request.Request(url, headers={'User-Agent': 'llmCode-CLI/1.0'})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _open_browser(url: str):
    """Opens URL in the user's default browser across Windows, macOS, and Linux."""
    try:
        if os.name == 'nt':
            os.startfile(url)  # type: ignore[attr-defined]
            return
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        else:
            for cmd in ('xdg-open', 'sensible-browser'):
                if shutil.which(cmd):
                    subprocess.Popen([cmd, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def cmd_setup(args):
    """Setup Python environment, compile/download backend binaries, install CLI agents, and sync config.json."""
    if getattr(args, 'uninstall', False):
        from src.build.setup_binaries import uninstall_all
        uninstall_all(non_interactive=getattr(args, 'non_interactive', False))
        return 0

    print(f"=== llmCode Setup ({sys.platform}) ===")
    server_dir = REPO_ROOT / 'src' / 'LLMServer'
    venv_dir = server_dir / '.venv'

    if not venv_dir.is_dir():
        print(f"Creating Python virtual environment in {venv_dir}...")
        subprocess.run([sys.executable, '-m', 'venv', str(venv_dir)], check=True)

    py_exe = get_python_exe()
    print("Installing dashboard requirements...")
    subprocess.run([py_exe, '-m', 'pip', 'install', '--upgrade', 'pip', '-q', '--no-warn-script-location'], check=True)
    subprocess.run([py_exe, '-m', 'pip', 'install', '-q', '--no-warn-script-location', '-r', str(server_dir / 'requirements.txt')], check=True)

    from src.build.setup_binaries import main as run_binary_setup
    run_binary_setup(non_interactive=getattr(args, 'non_interactive', False))

    sync_config(apply_hardware_defaults=True)
    from src.build.deploy_state import save_deploy_state
    save_deploy_state(REPO_ROOT)
    print(f"\nSetup completed. Configuration saved to: {CONFIG_FILE}")
    return 0


def cmd_check_update(args):
    """
    Analyzes current git status / file differences against last deployment state
    and executes only the necessary update steps (pip, config_sync, engines, restart_dashboard).
    """
    from src.build.deploy_state import diff_components, save_deploy_state
    res = diff_components(REPO_ROOT)

    if not res['has_prior_state']:
        print("No prior deployment state found (config/deploy_state.json). Running full setup...")
        return cmd_setup(args)

    steps = res['steps']
    changed = res['changed_files']

    if not steps:
        print("✓ All deployment components are up to date with repository state.")
        save_deploy_state(REPO_ROOT)
        return 0

    print("=== llmCode Incremental Update Check ===")
    print(f"Detected {len(changed)} changed file(s):")
    for f in changed[:10]:
        print(f"  • {f}")
    if len(changed) > 10:
        print(f"  ... and {len(changed) - 10} more.")

    print(f"\nAffected update step(s): {', '.join(steps)}")
    non_interactive = getattr(args, 'yes', False) or getattr(args, 'non_interactive', False)

    if 'full' in steps:
        print("\nCore setup script or CLI changed. Running full setup...")
        return cmd_setup(args)

    py_exe = get_python_exe()
    server_dir = REPO_ROOT / 'src' / 'LLMServer'

    if 'pip' in steps:
        print("\n[Step: pip] Updating dashboard requirements...")
        subprocess.run([py_exe, '-m', 'pip', 'install', '-q', '--no-warn-script-location', '-r', str(server_dir / 'requirements.txt')], check=True)

    if 'config_sync' in steps:
        print("\n[Step: config_sync] Synchronizing configuration database and paths...")
        sync_config(apply_hardware_defaults=True)

    if 'desktop_entry' in steps:
        from src.build.setup_binaries import render_desktop_entry
        print("\n[Step: desktop_entry] Updating desktop application entry...")
        render_desktop_entry(REPO_ROOT)

    if 'engines' in steps:
        print("\n[Step: engines] Backend engine build scripts changed.")
        print("  Estimated time: Compiling from source may take several minutes; prebuilt binaries take < 1 min.")
        proceed = True
        if not non_interactive:
            ans = input("Re-run backend engine setup now? [y/N]: ").strip().lower()
            proceed = ans in ('y', 'yes')
        if proceed:
            from src.build.setup_binaries import main as run_engine_setup
            run_engine_setup(non_interactive=non_interactive)
        else:
            print("Skipping engine update.")

    if 'restart_dashboard' in steps:
        # Check if services are currently running
        is_running = False
        if PID_FILE.is_file():
            try:
                pid = int(PID_FILE.read_text(encoding='utf-8').strip())
                is_running = is_pid_alive(pid)
            except Exception:
                pass

        if is_running:
            print("\n[Step: restart_dashboard] Server or common modules changed and dashboard daemon is active.")
            restart = True
            if not non_interactive:
                ans = input("Restart dashboard services now to load updated code? [y/N]: ").strip().lower()
                restart = ans in ('y', 'yes')
            if restart:
                print("Restarting dashboard daemon...")
                cmd_stop(args)
                time.sleep(1)
                cmd_dash(args)
            else:
                print("Dashboard restart skipped. Changes will take effect on next launch.")

    save_deploy_state(REPO_ROOT)
    print("\n✓ Incremental update completed successfully.")
    return 0


def cmd_download(args):
    """Download recommended models or Hugging Face GGUF files."""
    sync_config()
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
        req = urllib.request.Request(url, headers={'User-Agent': 'llmCode/1.0'})
        with urllib.request.urlopen(req) as resp, open(dest, 'wb') as f:
            shutil.copyfileobj(resp, f)

    print(f"Download complete: {dest}")
    return 0


def cmd_serve(args):
    """Serve a local GGUF model via llama-server."""
    sync_config()
    llama_bin = get_llama_server_binary()
    if not llama_bin.is_file():
        print(f"Error: llama-server binary not found at {llama_bin}. Run setup first.")
        return 1

    host = getattr(args, 'host', '127.0.0.1')
    if host not in ('127.0.0.1', 'localhost'):
        print(f"Notice: Binding server to '{host}' (LAN accessible).")

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

    parallel_slots = max(1, int(getattr(args, 'parallel', 1) or 1))
    effective_ctx = int(args.context) * parallel_slots
    threads_batch = int(getattr(args, 'threads_batch', args.threads) or args.threads)

    print(f"Serving model: {model_file.name}")
    print(f"Offloading {args.layers} GPU layers | Per-slot Context: {args.context} (Total KV: {effective_ctx} across {parallel_slots} slot(s)) | Batch: {args.batch} | Port: {args.port}")

    cmd = [
        str(llama_bin),
        '--model', str(model_file),
        '--alias', args.alias or 'llamacpp',
        '--host', host,
        '--port', str(args.port),
        '--ctx-size', str(effective_ctx),
        '--n-gpu-layers', str(args.layers),
        '--threads', str(args.threads),
        '--threads-batch', str(threads_batch),
        '-b', str(args.batch),
        '-ub', str(args.ubatch),
        '--temp', str(args.temp),
        '--top-p', str(args.top_p),
        '--top-k', str(args.top_k),
        '--min-p', str(args.min_p),
        '--repeat-penalty', str(args.repeat_penalty),
        '--parallel', str(parallel_slots),
        '--jinja',
        '-fa', args.flash_attn,
    ]
    if parallel_slots > 1:
        cmd.append('--cont-batching')
    if args.kv_cache != 'f16':
        cmd += ['-ctk', args.kv_cache, '-ctv', args.kv_cache]
    if args.custom_flags:
        raw_extra = [c for c in args.custom_flags.split() if c.strip()]
        if parallel_slots > 1:
            cleaned_extra = []
            i = 0
            while i < len(raw_extra):
                if raw_extra[i] == '--spec-type':
                    i += 2 if (i + 1 < len(raw_extra) and not raw_extra[i + 1].startswith('-')) else 1
                    continue
                if raw_extra[i].startswith('--spec-type='):
                    i += 1
                    continue
                cleaned_extra.append(raw_extra[i])
                i += 1
            raw_extra = cleaned_extra
        cmd.extend(raw_extra)

    bin_parent = str(llama_bin.resolve().parent)
    env = build_binary_env(llama_bin)

    try:
        subprocess.run(cmd, cwd=bin_parent, env=env)
    except KeyboardInterrupt:
        print("\nServer stopped.")
    return 0


def cmd_dash(args):
    """Launch the Web Dashboard (FastAPI server on port 7860) in foreground or background daemon mode."""
    cfg = sync_config()
    port = args.port or cfg.get('settings', {}).get('dash_port', 7860)
    host = getattr(args, "host", "127.0.0.1")
    if host not in ('127.0.0.1', 'localhost'):
        print(f"WARNING: Binding dashboard to '{host}' exposes dashboard controls without authentication. Restrict access or use a firewall.")
    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"

    # Check if already running
    if PID_FILE.is_file():
        try:
            old_pid = int(PID_FILE.read_text(encoding='utf-8').strip())
            if is_pid_alive(old_pid) and _is_dash_healthy(port):
                print(f"llm-dash already running (PID {old_pid}) at {url}")
                if not args.no_browser:
                    _open_browser(url)
                return 0
        except Exception:
            pass
    if _is_dash_healthy(port):
        print(f"llm-dash already running at {url}")
        if not args.no_browser:
            _open_browser(url)
        return 0

    server_dir = REPO_ROOT / 'src' / 'LLMServer'
    py_exe = get_python_exe()
    cmd = [
        py_exe, '-m', 'uvicorn', 'app:app',
        '--app-dir', str(server_dir),
        '--host', host,
        '--port', str(port),
    ]

    if args.daemon:
        DAEMON_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_f = open(DAEMON_LOG_FILE, 'a', encoding='utf-8')
        popen_kwargs = {
            'stdin': subprocess.DEVNULL,
            'stdout': log_f,
            'stderr': subprocess.STDOUT,
            'cwd': str(REPO_ROOT),
        }
        if os.name == 'nt':
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP on Windows
            popen_kwargs['creationflags'] = 0x00000008 | 0x00000200
        else:
            popen_kwargs['start_new_session'] = True

        proc = subprocess.Popen(cmd, **popen_kwargs)
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(f"{proc.pid}\n", encoding='utf-8')
        print(f"Started llm-dash daemon (PID {proc.pid}) on {url}")

        # Wait up to 10s for health check
        for _ in range(20):
            if _is_dash_healthy(port):
                print(f"llm-dash ready at {url}")
                break
            time.sleep(0.5)

        if not args.no_browser:
            _open_browser(url)
        return 0
    else:
        if not args.no_browser:
            _open_browser(url)
        print(f"Starting dashboard at {url} ...")
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            print("\nDashboard stopped.")
        return 0


def cmd_stop(args):
    """Stop llm-dash daemon and any running inference engines."""
    res = stop_all_services()
    stopped = res.get('stopped', [])
    if stopped:
        print(f"Stopped services: {', '.join(stopped)}")
    else:
        print("No active llm-dash or inference engine services found.")
    return 0


def cmd_aider(args):
    """Spawn Aider connected to the running local server in an interactive terminal."""
    if not AGENT_ENV_FILE.is_file():
        print(f"Error: Agent environment credentials not found at {AGENT_ENV_FILE}.")
        print("Deploy a backend engine first via web dashboard before launching Aider.")
        return 1
    cfg = sync_config()
    aider_bin = cfg.get('agents', {}).get('aider') or 'aider'
    alias = cfg.get('settings', {}).get('last_alias') or 'llamacpp'
    target_dir = Path(args.dir).expanduser().resolve()
    aider_cmd = f'"{aider_bin}" --env-file "{AGENT_ENV_FILE}" --model openai/{alias} --stream'
    print(f"Launching Aider in {target_dir} (model: openai/{alias})...")
    spawn_in_terminal(aider_cmd, cwd=target_dir, title="llmCode Aider")
    return 0


def cmd_sequence(args):
    """Run a batch of prompts in parallel across active server slots via /api/sequence/run."""
    import json
    cfg = sync_config()
    dash_port = args.dash_port or cfg.get('settings', {}).get('dash_port', 7860)
    items = [{'id': f'slot-{i + 1}', 'prompt': p, 'max_tokens': args.max_tokens} for i, p in enumerate(args.prompts) if p.strip()]
    if not items:
        print("Error: Provide at least one non-empty prompt.")
        return 1

    payload = {
        'items': items,
        'max_concurrency': args.concurrency,
        'join_policy': args.policy,
        'timeout_sec': args.timeout,
    }
    url = f"http://127.0.0.1:{dash_port}/api/sequence/run"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': 'llmCode-CLI/1.0'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=args.timeout + 10) as resp:
            summary = json.loads(resp.read(10_000_000))
    except Exception as e:
        print(f"Error calling parallel sequence runner at {url}: {e}")
        return 1

    print(f"=== Parallel Sequence [{summary.get('run_id')}] Verdict: {summary.get('verdict', '').upper()} ===")
    print(f"Concurrency: {summary.get('concurrency')} | Succeeded: {summary.get('succeeded')}/{summary.get('total_items')} | Total: {summary.get('total_latency_ms')}ms\n")
    for item in summary.get('results', []):
        print(f"--- [{item['id']}] ({item['status'].upper()} - {item['latency_ms']}ms) ---")
        if item['status'] == 'ok':
            print(item.get('content', '').strip())
        else:
            print(f"ERROR: {item.get('error')}")
        print()
    return 0 if summary.get('ok') else 1


def main():
    sanitize_system_path()
    parser = argparse.ArgumentParser(description="llmCode - Cross-Platform Local LLM Runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # setup
    p_setup = subparsers.add_parser("setup", help="Set up environment, backend engines, and CLI agents")
    p_setup.add_argument("-y", "--non-interactive", action="store_true", help="Run setup with default choices without prompting")
    p_setup.add_argument("--uninstall", action="store_true", help="Completely uninstall all llmCode components, binaries, venv, configs, and models")
    p_setup.set_defaults(func=cmd_setup)

    # check-update
    p_check = subparsers.add_parser("check-update", help="Check for git/file updates and run incremental deployment steps")
    p_check.add_argument("-y", "--yes", action="store_true", help="Automatically accept recommended update steps without prompting")
    p_check.add_argument("--non-interactive", action="store_true", help="Run without interactive confirmation")
    p_check.set_defaults(func=cmd_check_update)

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
    p_serve.add_argument("--context", type=int, default=8192, help="Per-slot context size")
    p_serve.add_argument("--parallel", type=int, default=1, help="Parallel decoding slots (--parallel N)")
    p_serve.add_argument("--threads", type=int, default=4, help="CPU threads")
    p_serve.add_argument("--threads-batch", type=int, default=6, help="CPU threads for batch processing")
    p_serve.add_argument("--batch", type=int, default=2048, help="Batch size")
    p_serve.add_argument("--ubatch", type=int, default=256, help="Physical micro-batch size")
    p_serve.add_argument("--temp", type=float, default=0.2, help="Sampling temperature")
    p_serve.add_argument("--top-p", type=float, default=0.95, help="Top-p sampling")
    p_serve.add_argument("--top-k", type=int, default=40, help="Top-k sampling")
    p_serve.add_argument("--min-p", type=float, default=0.05, help="Min-p sampling")
    p_serve.add_argument("--repeat-penalty", type=float, default=1.05, help="Repetition penalty")
    p_serve.add_argument("--flash-attn", default="on", choices=["on", "off"], help="Flash attention")
    p_serve.add_argument("--kv-cache", default="q8_0", choices=["f16", "q8_0", "q4_0"], help="KV cache quantization")
    p_serve.add_argument("--port", type=int, default=8080, help="Port to listen on")
    p_serve.add_argument("--host", default="127.0.0.1", help="Host address to bind to (default: 127.0.0.1)")
    p_serve.add_argument("--alias", default="", help="Model alias")
    p_serve.add_argument("--custom-flags", default="", help="Additional raw flags to pass")
    p_serve.set_defaults(func=cmd_serve)

    # dash
    p_dash = subparsers.add_parser("dash", help="Start Web Dashboard (llm-dash)")
    p_dash.add_argument("--port", type=int, default=7860, help="Dashboard port")
    p_dash.add_argument("--host", default="127.0.0.1", help="Host address to bind to (default: 127.0.0.1)")
    p_dash.add_argument("--daemon", action="store_true", help="Run dashboard as background daemon")
    p_dash.add_argument("--no-browser", action="store_true", help="Do not auto-open browser")
    p_dash.set_defaults(func=cmd_dash)

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop llm-dash and active backend servers")
    p_stop.set_defaults(func=cmd_stop)

    # aider
    p_aider = subparsers.add_parser("aider", help="Launch Aider CLI")
    p_aider.add_argument("--dir", default=".", help="Project directory")
    p_aider.add_argument("--port", type=int, default=8080, help="Server port")
    p_aider.set_defaults(func=cmd_aider)

    # sequence
    p_seq = subparsers.add_parser("sequence", help="Run multiple prompts in parallel across active server slots")
    p_seq.add_argument("prompts", nargs="+", help="Prompts to execute concurrently")
    p_seq.add_argument("--concurrency", type=int, default=0, help="Max concurrent workers (0 = match active --parallel slots)")
    p_seq.add_argument("--policy", default="all_required", choices=["all_required", "best_effort"], help="Join policy for parallel tasks")
    p_seq.add_argument("--max-tokens", type=int, default=1024, help="Max tokens per task")
    p_seq.add_argument("--timeout", type=int, default=300, help="Timeout in seconds per task")
    p_seq.add_argument("--dash-port", type=int, default=7860, help="Dashboard port")
    p_seq.set_defaults(func=cmd_sequence)

    args = parser.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main() or 0)
