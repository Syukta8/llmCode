# -*- coding: utf-8 -*-
"""
Deployment state tracker and incremental update analyzer for llmCode.
Compares repository state after git pull (or manual update) against the last known
deployment state (config/deploy_state.json), identifying affected steps (pip, config_sync,
engines, restart_dashboard, or full setup) to minimize rebuild overhead.
"""
import fnmatch
import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from src.common.paths import DEPLOY_STATE_FILE, REPO_ROOT

# Map glob patterns relative to REPO_ROOT to execution steps
COMPONENT_RULES: List[Tuple[str, str]] = [
    ('setup.sh', 'full'),
    ('setup.bat', 'full'),
    ('llmcode.py', 'full'),
    ('src/LLMServer/requirements.txt', 'pip'),
    ('src/build/setup_binaries.py', 'engines'),
    ('src/common/paths.py', 'config_sync'),
    ('src/LLMServer/**', 'restart_dashboard'),
    ('src/common/**', 'restart_dashboard'),
    ('bin/llmCode.desktop.in', 'desktop_entry'),
]

WATCHED_PATTERNS = [rule[0] for rule in COMPONENT_RULES]


def get_git_head_sha(repo_root: Path = REPO_ROOT) -> Optional[str]:
    """Retrieves current git HEAD commit hash, or None if git not present."""
    try:
        res = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return None


def hash_file(file_path: Path) -> Optional[str]:
    """Computes SHA-256 hash of a file."""
    if not file_path.is_file():
        return None
    h = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def collect_file_hashes(repo_root: Path = REPO_ROOT) -> Dict[str, str]:
    """Hashes all watched component files across the repository."""
    hashes: Dict[str, str] = {}
    for root, _, files in os.walk(repo_root):
        rel_root = Path(root).relative_to(repo_root)
        if any(part in ('.git', '.venv', 'venv', '__pycache__', 'models', 'logs') for part in rel_root.parts):
            continue
        for file in files:
            rel_file = (rel_root / file).as_posix()
            if any(fnmatch.fnmatch(rel_file, pattern) for pattern in WATCHED_PATTERNS):
                full_path = repo_root / rel_file
                f_hash = hash_file(full_path)
                if f_hash:
                    hashes[rel_file] = f_hash
    return hashes


def load_deploy_state(repo_root: Path = REPO_ROOT) -> Optional[Dict]:
    """Loads previously saved deployment state from config/deploy_state.json relative to repo_root."""
    state_file = repo_root / 'config' / 'deploy_state.json'
    if not state_file.is_file():
        return None
    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def save_deploy_state(repo_root: Path = REPO_ROOT) -> Dict:
    """Records current git commit hash and watched file hashes into config/deploy_state.json."""
    state_file = repo_root / 'config' / 'deploy_state.json'
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state = {
        'sha': get_git_head_sha(repo_root),
        'file_hashes': collect_file_hashes(repo_root),
        'timestamp': datetime.now().isoformat(timespec='seconds'),
    }
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    return state


def _git_changed_files(old_sha: str, repo_root: Path) -> Optional[List[str]]:
    """Retrieves list of changed files between old_sha and HEAD + unstaged changes."""
    try:
        # Check if old_sha exists in git history
        cat_proc = subprocess.run(
            ['git', 'cat-file', '-e', f'{old_sha}^{{commit}}'],
            cwd=str(repo_root),
            capture_output=True,
            check=False,
            timeout=5,
        )
        if cat_proc.returncode != 0:
            return None

        # Committed changes between old_sha and HEAD
        diff_proc = subprocess.run(
            ['git', 'diff', '--name-only', old_sha, 'HEAD'],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if diff_proc.returncode != 0:
            return None

        files = set(filter(None, diff_proc.stdout.splitlines()))

        # Add dirty / uncommitted modifications
        status_proc = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if status_proc.returncode == 0:
            for line in status_proc.stdout.splitlines():
                if len(line) > 3:
                    files.add(line[3:].strip())

        return sorted(list(files))
    except Exception:
        return None


def diff_components(repo_root: Path = REPO_ROOT) -> Dict:
    """
    Compares current repository state against the last saved deployment state.
    Returns:
        dict: {
            'has_prior_state': bool,
            'changed_files': list[str],
            'steps': list[str], # ordered execution steps: pip, config_sync, engines, restart_dashboard, full
        }
    """
    prior_state = load_deploy_state(repo_root)
    if not prior_state:
        return {
            'has_prior_state': False,
            'changed_files': [],
            'steps': ['full'],
        }

    changed_files: List[str] = []
    old_sha = prior_state.get('sha')

    if old_sha:
        git_files = _git_changed_files(old_sha, repo_root)
        if git_files is not None:
            changed_files = git_files

    # Fallback to file hash comparison if git is unavailable or old commit missing
    if not changed_files and not (old_sha and git_files == []):
        current_hashes = collect_file_hashes(repo_root)
        old_hashes = prior_state.get('file_hashes', {})
        all_keys = set(current_hashes.keys()) | set(old_hashes.keys())
        diff_keys = [k for k in all_keys if current_hashes.get(k) != old_hashes.get(k)]
        changed_files = sorted(diff_keys)

    # Map files to steps
    steps_set: Set[str] = set()
    for f in changed_files:
        norm_f = f.replace('\\', '/')
        for pattern, step in COMPONENT_RULES:
            if fnmatch.fnmatch(norm_f, pattern):
                steps_set.add(step)

    # Order steps logically: full overrides all; otherwise pip -> config_sync -> desktop_entry -> engines -> restart_dashboard
    if 'full' in steps_set:
        ordered_steps = ['full']
    else:
        desired_order = ['pip', 'config_sync', 'desktop_entry', 'engines', 'restart_dashboard']
        ordered_steps = [s for s in desired_order if s in steps_set]

    return {
        'has_prior_state': True,
        'changed_files': changed_files,
        'steps': ordered_steps,
    }
