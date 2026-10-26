# -*- coding: utf-8 -*-
"""
Cross-platform process lifecycle and terminal management for llmCode.
Uniformly spawns background servers, kills child process trees, and launches CLI tools
in interactive terminal windows on Windows, Linux, and macOS.
"""
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

from src.common.paths import PID_FILE


def detect_terminal() -> Optional[str]:
    """Detect available terminal emulator on Windows, macOS, or Linux."""
    if os.name == 'nt':
        for term in ('wt.exe', 'cmd.exe', 'powershell.exe'):
            if shutil.which(term):
                return term
        return 'cmd.exe'
    elif sys.platform == 'darwin':
        if Path('/Applications/iTerm.app').exists():
            return 'iTerm.app'
        return 'Terminal.app'
    else:
        for term in ('gnome-terminal', 'xfce4-terminal', 'konsole', 'x-terminal-emulator', 'xterm'):
            if shutil.which(term):
                return term
        return None


def spawn_in_terminal(command_str: str, cwd: Path, title: str = "llmCode") -> subprocess.Popen:
    """
    Launch an interactive command inside a new terminal window across Windows, macOS, and Linux.
    Keeps terminal open on exit so the user can inspect output or continue interacting.
    """
    terminal = detect_terminal()
    if not terminal:
        raise RuntimeError("No supported terminal emulator found on this system.")

    cwd_str = str(cwd.resolve())

    if os.name == 'nt':
        if terminal.lower() == 'wt.exe':
            args = ['wt.exe', '-d', cwd_str, 'cmd.exe', '/k', command_str]
        elif terminal.lower() == 'powershell.exe':
            args = ['powershell.exe', '-NoExit', '-Command', f"Set-Location '{cwd_str}'; {command_str}"]
        else:
            args = ['cmd.exe', '/c', 'start', title, '/D', cwd_str, 'cmd.exe', '/k', command_str]
        return subprocess.Popen(args, cwd=cwd_str, shell=True)

    elif sys.platform == 'darwin':
        escaped_cwd = cwd_str.replace('"', '\\"')
        escaped_cmd = command_str.replace('"', '\\"')
        script = f'tell application "Terminal"\nactivate\ndo script "cd \\"{escaped_cwd}\\" && {escaped_cmd}"\nend tell'
        return subprocess.Popen(['osascript', '-e', script],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)

    else:
        if terminal == 'gnome-terminal':
            term_args = ['gnome-terminal', '--', 'bash', '-c', f'cd "{cwd_str}" && {command_str}; exec bash']
        elif terminal == 'xfce4-terminal':
            term_args = ['xfce4-terminal', '-e', f'bash -c "cd \\"{cwd_str}\\" && {command_str}; exec bash"']
        elif terminal == 'konsole':
            term_args = ['konsole', '--workdir', cwd_str, '-e', 'bash', '-c', f'{command_str}; exec bash']
        elif terminal == 'x-terminal-emulator':
            term_args = ['x-terminal-emulator', '-e', f'bash -c "cd \\"{cwd_str}\\" && {command_str}; exec bash"']
        else:
            term_args = ['xterm', '-e', f'bash -c "cd \\"{cwd_str}\\" && {command_str}; exec bash"']
        return subprocess.Popen(term_args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)


def is_pid_alive(pid: int) -> bool:
    """Checks whether a process ID is currently alive across platforms."""
    if pid <= 0:
        return False
    if os.name == 'nt':
        try:
            res = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                capture_output=True, text=True, timeout=4
            )
            return str(pid) in res.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False


def kill_process_tree(pid: int, timeout: int = 8) -> bool:
    """Cleanly terminate a process and all its children across Windows, Linux, and macOS."""
    if pid <= 0:
        return False
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                           capture_output=True, timeout=timeout)
            return True
        except Exception:
            return False
    else:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            return True
        except ProcessLookupError:
            return True
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
                return True
            except ProcessLookupError:
                return True
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                    return True
                except Exception:
                    return False


def stop_all_services() -> dict:
    """
    Stops the llm-dash daemon (via PID_FILE and process pattern) and any active
    inference engine processes (llama-server, koboldcpp, ollama serve) across
    Windows, Linux, and macOS.
    """
    stopped = []

    # 1. Stop PID recorded in config/llmserver.pid
    if PID_FILE.is_file():
        try:
            pid_str = PID_FILE.read_text(encoding='utf-8').strip()
            if pid_str.isdigit():
                pid = int(pid_str)
                if is_pid_alive(pid):
                    kill_process_tree(pid)
                    stopped.append(f"llm-dash (PID {pid})")
        except Exception:
            pass
        PID_FILE.unlink(missing_ok=True)

    # 2. Platform-specific sweep for orphaned dashboard or backend processes
    if os.name == 'nt':
        for img in ('llama-server.exe', 'koboldcpp.exe'):
            res = subprocess.run(['taskkill', '/F', '/IM', img], capture_output=True, text=True)
            if res.returncode == 0:
                stopped.append(img)
        # Also kill uvicorn app:app processes via PowerShell / WMI filter
        ps_cmd = (
            "Get-CimInstance Win32_Process | Where-Object { "
            "$_.CommandLine -match 'uvicorn.*app:app' -or $_.CommandLine -match 'koboldcpp\\.py' "
            "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd], capture_output=True)
    else:
        patterns = [
            ('llm-dash', 'uvicorn app:app'),
            ('llama-server', 'llama-server'),
            ('koboldcpp', 'koboldcpp.py'),
        ]
        for label, pat in patterns:
            res = subprocess.run(['pgrep', '-f', pat], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                subprocess.run(['pkill', '-f', pat], capture_output=True)
                stopped.append(label)

    return {'stopped': stopped}
