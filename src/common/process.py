# -*- coding: utf-8 -*-
"""
Cross-platform process lifecycle and terminal management for llmCode.
Uniformly spawns background servers, kills child processes, and launches CLI tools
in interactive terminal windows on Linux and Windows.
"""
import os
import sys
import shutil
import signal
import subprocess
from pathlib import Path
from typing import List, Optional

def detect_terminal() -> Optional[str]:
    """Detect available terminal emulator on Linux or Windows."""
    if os.name == 'nt':
        for term in ['wt.exe', 'cmd.exe', 'powershell.exe']:
            if shutil.which(term):
                return term
        return 'cmd.exe'
    else:
        for term in ['gnome-terminal', 'xfce4-terminal', 'x-terminal-emulator', 'xterm']:
            if shutil.which(term):
                return term
        return None

def spawn_in_terminal(command_str: str, cwd: Path, title: str = "llmCode") -> subprocess.Popen:
    """
    Launch an interactive command inside a new terminal window across Linux and Windows.
    Keeps terminal open on exit.
    """
    terminal = detect_terminal()
    if not terminal:
        raise RuntimeError("No supported terminal emulator found on this system.")

    cwd_str = str(cwd.resolve())

    if os.name == 'nt':
        if terminal.lower() == 'wt.exe':
            # Windows Terminal
            args = ['wt.exe', '-d', cwd_str, 'cmd.exe', '/k', command_str]
        elif terminal.lower() == 'powershell.exe':
            args = ['powershell.exe', '-NoExit', '-Command', f"Set-Location '{cwd_str}'; {command_str}"]
        else:
            # cmd.exe
            args = ['cmd.exe', '/c', 'start', title, '/D', cwd_str, 'cmd.exe', '/k', command_str]
        return subprocess.Popen(args, cwd=cwd_str, shell=True)
    else:
        # Linux terminals
        if terminal == 'gnome-terminal':
            term_args = ['gnome-terminal', '--', 'bash', '-c', f'cd "{cwd_str}" && {command_str}; exec bash']
        elif terminal == 'xfce4-terminal':
            term_args = ['xfce4-terminal', '-e', f'bash -c "cd \\"{cwd_str}\\" && {command_str}; exec bash"']
        elif terminal == 'x-terminal-emulator':
            term_args = ['x-terminal-emulator', '-e', f'bash -c "cd \\"{cwd_str}\\" && {command_str}; exec bash"']
        else:
            term_args = ['xterm', '-e', f'bash -c "cd \\"{cwd_str}\\" && {command_str}; exec bash"']
        return subprocess.Popen(term_args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)

def kill_process_tree(pid: int, timeout: int = 8) -> bool:
    """Cleanly terminate a process and all its children across OSes."""
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                           capture_output=True, timeout=timeout)
            return True
        except Exception:
            return False
    else:
        try:
            # Send SIGTERM to process group
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            return True
        except ProcessLookupError:
            return True
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
                return True
            except Exception:
                return False
