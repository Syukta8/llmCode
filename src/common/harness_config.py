"""
Non-destructive writers for global harness configs (~/.codex/llmcode.config.toml).

Why: users often run the same CLIs against their cloud accounts. Modern Codex CLI (v0.167+)
requires separate profile files (`~/.codex/<profile>.config.toml`) with top-level keys rather than
embedding `[profiles.<name>]` in the base `config.toml`. llmCode writes `~/.codex/llmcode.config.toml`
and cleans any legacy managed block from `~/.codex/config.toml` with timestamped backups.
"""
import json
import re
import shutil
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src.common.harness import CODEX_KEY_ENV, CODEX_PROFILE

BLOCK_BEGIN = '# >>> llmcode managed block (do not edit; rewritten on every launch) >>>'
BLOCK_END = '# <<< llmcode managed block <<<'

# Shared MCP catalogue (single source of truth for OpenCode and Codex); all disabled by default.
DEFAULT_MCP_SERVERS: Dict[str, List[str]] = {
    'fetch': ['uvx', 'mcp-server-fetch'],
    'context7': ['npx', '-y', '@upstash/context7-mcp'],
    'sequential-thinking': ['npx', '-y', '@modelcontextprotocol/server-sequential-thinking'],
    'playwright': ['npx', '-y', '@playwright/mcp@latest'],
}


def backup(path: Path) -> Optional[Path]:
    """Copies `path` to `path.bak-YYYYmmdd-HHMMSS` when it exists; returns the backup path."""
    if not path.is_file():
        return None
    dest = path.with_name(f'{path.name}.bak-{datetime.now().strftime("%Y%m%d-%H%M%S")}')
    shutil.copy2(path, dest)
    return dest


def _strip_block(text: str) -> str:
    pattern = re.compile(rf'\n?{re.escape(BLOCK_BEGIN)}.*?{re.escape(BLOCK_END)}\n?', re.S)
    return pattern.sub('\n', text).rstrip('\n')


def _toml_str(value: str) -> str:
    return json.dumps(value)


def cleanup_legacy_codex_config(home: Path) -> Optional[Path]:
    """Strips legacy llmcode block from `<home>/.codex/config.toml` to prevent Codex profile conflict."""
    cfg = home / '.codex' / 'config.toml'
    if not cfg.is_file():
        return None
    original = cfg.read_text(encoding='utf-8')
    cleaned = _strip_block(original)
    if cleaned != original:
        bak = backup(cfg)
        cfg.write_text(cleaned + ('\n' if cleaned else ''), encoding='utf-8')
        return bak
    return None


def write_codex_profile(home: Path, base_url: str, alias: str, context: int = 0) -> Path:
    """
    Writes dedicated profile `<home>/.codex/llmcode.config.toml` and cleans legacy blocks from `config.toml`.
    Codex v0.167+ overlays `<home>/.codex/<profile>.config.toml` on top of `config.toml`.
    """
    codex_dir = home / '.codex'
    codex_dir.mkdir(parents=True, exist_ok=True)
    cleanup_legacy_codex_config(home)

    cfg = codex_dir / f'{CODEX_PROFILE}.config.toml'
    original = cfg.read_text(encoding='utf-8') if cfg.is_file() else ''

    lines = [
        BLOCK_BEGIN,
        f'model = {_toml_str(alias)}',
        f'model_provider = {_toml_str(CODEX_PROFILE)}',
    ]
    if context > 0:
        lines.append(f'model_context_window = {int(context)}')

    lines += [
        '',
        f'[model_providers.{CODEX_PROFILE}]',
        f'name = {_toml_str("llmCode local (" + alias + ")")}',
        f'base_url = {_toml_str(base_url.rstrip("/") + "/v1")}',
        f'env_key = {_toml_str(CODEX_KEY_ENV)}',
        'wire_api = "responses"',
        'stream_idle_timeout_ms = 600000',
    ]

    for name, argv in DEFAULT_MCP_SERVERS.items():
        lines += ['', f'[mcp_servers.{_toml_str(name)}]', f'command = {_toml_str(argv[0])}',
                  f'args = [{", ".join(_toml_str(a) for a in argv[1:])}]', 'enabled = false']
    lines.append(BLOCK_END)

    new_text = '\n'.join(lines) + '\n'
    tomllib.loads(new_text)  # Fail before writing if invalid TOML

    if new_text != original:
        if original:
            backup(cfg)
        cfg.write_text(new_text, encoding='utf-8')
    return cfg
