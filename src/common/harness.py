"""
Harness registry for CLI coding agents (OpenCode, Claude Code, Codex).

Why: every harness needs the same lifecycle (discover -> configure -> spawn in terminal -> track),
but differs in wire protocol and flags. Centralizing that here keeps llmcode.py and app.py
protocol-agnostic and makes adding/removing a harness a one-entry change.
"""
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from src.common.paths import API_KEY_FILE


@dataclass(frozen=True)
class Harness:
    """Static description of a harness; `api_style` selects which local endpoint it must talk to."""
    id: str
    label: str
    api_style: str  # 'openai' (chat completions), 'anthropic' (/v1/messages), 'responses' (/v1/responses)


HARNESSES: Dict[str, Harness] = {
    'opencode': Harness('opencode', 'OpenCode', 'openai'),
    'claude': Harness('claude', 'Claude Code', 'anthropic'),
    'codex': Harness('codex', 'Codex', 'responses'),
}

CODEX_PROFILE = 'llmcode'
CODEX_KEY_ENV = 'LLMCODE_API_KEY'
# Claude Code refuses to start against tiny windows; below this the system prompt + tools alone overflow.
CLAUDE_MIN_CONTEXT = 32768


def get_harness(harness_id: str) -> Harness:
    """Returns the registry entry or raises ValueError so API/CLI layers can map it to a 400."""
    h = HARNESSES.get(harness_id)
    if not h:
        raise ValueError(f"Unknown harness '{harness_id}'. Choose one of: {', '.join(HARNESSES)}")
    return h


def claude_env(base_url: str, alias: str) -> Dict[str, str]:
    """Env that points Claude Code at the local Anthropic-compatible endpoint; every model tier maps to the one local alias."""
    return {
        'ANTHROPIC_BASE_URL': base_url,
        'ANTHROPIC_MODEL': alias,
        'ANTHROPIC_SMALL_FAST_MODEL': alias,
        'ANTHROPIC_DEFAULT_OPUS_MODEL': alias,
        'ANTHROPIC_DEFAULT_SONNET_MODEL': alias,
        'ANTHROPIC_DEFAULT_HAIKU_MODEL': alias,
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
        'DISABLE_TELEMETRY': '1',
        'API_TIMEOUT_MS': '600000',
    }


def _env_prefix(env: Dict[str, str], key_var: Optional[str], key_file: Path) -> str:
    """
    Builds a shell prefix exporting `env` plus `key_var` read from `key_file` at runtime.
    Inline (not Popen env) because terminal servers like gnome-terminal do not inherit the caller env;
    the key is read from file so it never appears in process listings.
    """
    if os.name == 'nt':
        parts = [f'set "{k}={v}"' for k, v in env.items()]
        if key_var:
            parts.append(f'set /p {key_var}=<"{key_file}"')
        return ' && '.join(parts) + (' && ' if parts else '')
    parts = [f'export {k}={shlex.quote(v)}' for k, v in env.items()]
    if key_var:
        parts.append(f'export {key_var}="$(cat {shlex.quote(str(key_file))})"')
    return '; '.join(parts) + ('; ' if parts else '')


def _quote_bin(bin_path: str) -> str:
    return f'"{bin_path}"' if ' ' in bin_path else bin_path


def build_launch_command(
    harness_id: str,
    bin_path: str,
    alias: str,
    base_url: str,
    restore: bool = False,
    session_id: str = '',
    key_file: Path = API_KEY_FILE,
) -> str:
    """Returns the full terminal command (env prefix + CLI) for Claude Code or Codex."""
    session_id = session_id.strip()
    if session_id and not all(c.isalnum() or c in '-_' for c in session_id):
        raise ValueError('session_id may only contain letters, digits, "-" and "_"')
    exe = _quote_bin(bin_path)
    if harness_id == 'claude':
        env = claude_env(base_url, alias)
        cmd = exe
        if session_id:
            cmd += f' --resume {session_id}'
        elif restore:
            cmd += ' --continue'
        unset = '' if os.name == 'nt' else 'unset ANTHROPIC_API_KEY; '
        if os.name == 'nt':
            env = {'ANTHROPIC_API_KEY': '', **env}
        return unset + _env_prefix(env, 'ANTHROPIC_AUTH_TOKEN', key_file) + cmd
    if harness_id == 'codex':
        cmd = f'{exe} --profile {CODEX_PROFILE}'
        if session_id:
            cmd = f'{exe} resume {session_id} --profile {CODEX_PROFILE}'
        elif restore:
            cmd = f'{exe} resume --last --profile {CODEX_PROFILE}'
        return _env_prefix({}, CODEX_KEY_ENV, key_file) + cmd
    raise ValueError(f"build_launch_command does not handle harness '{harness_id}'")


def context_warning(harness_id: str, context: int) -> Optional[str]:
    """Advisory surfaced to the user when the deployed window is too small for the harness system prompt."""
    if harness_id in ('claude', 'codex') and context < CLAUDE_MIN_CONTEXT:
        return f'{HARNESSES[harness_id].label} needs >= {CLAUDE_MIN_CONTEXT} context (current {context}); expect truncation errors.'
    return None
