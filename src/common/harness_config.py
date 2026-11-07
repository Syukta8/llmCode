"""
Non-destructive writers for global harness configs (~/.codex/llmcode.config.toml, ~/.claude/settings.json).

Why: users often run the same CLIs against their cloud accounts. Modern Codex CLI (v0.167+)
requires separate profile files (`~/.codex/<profile>.config.toml`) with top-level keys rather than
embedding `[profiles.<name>]` in the base `config.toml`. llmCode writes `~/.codex/llmcode.config.toml`
and configures `~/.codex/config.toml` and Claude Code's `~/.claude/settings.json` with timestamped backups.
"""
import json
import re
import shutil
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def write_claude_settings(home: Path, base_url: str, alias: str, context: int = 0) -> Path:
    """
    Writes or merges global Claude Code settings at `<home>/.claude/settings.json`.
    Sets permissions to bypassPermissions per official docs:
      - defaultMode: "bypassPermissions"
      - permissions: {"defaultMode": "bypassPermissions"}
    Sets model to alias (e.g. llamacpp) and configures env (ANTHROPIC_BASE_URL, ANTHROPIC_MODEL, etc.).
    Preserves other user-defined settings and creates timestamped backup.
    """
    claude_dir = home / '.claude'
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_file = claude_dir / 'settings.json'

    data: Dict[str, Any] = {}
    original = ''
    if settings_file.is_file():
        original = settings_file.read_text(encoding='utf-8')
        try:
            parsed = json.loads(original)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            pass

    data['defaultMode'] = 'bypassPermissions'
    if not isinstance(data.get('permissions'), dict):
        data['permissions'] = {}
    data['permissions']['defaultMode'] = 'bypassPermissions'
    data['model'] = alias

    env = data.get('env')
    if not isinstance(env, dict):
        env = {}
    env.update({
        'ANTHROPIC_BASE_URL': base_url,
        'ANTHROPIC_MODEL': alias,
        'ANTHROPIC_SMALL_FAST_MODEL': alias,
        'ANTHROPIC_DEFAULT_OPUS_MODEL': alias,
        'ANTHROPIC_DEFAULT_SONNET_MODEL': alias,
        'ANTHROPIC_DEFAULT_HAIKU_MODEL': alias,
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
        'DISABLE_TELEMETRY': '1',
        'API_TIMEOUT_MS': '600000',
    })
    data['env'] = env

    new_text = json.dumps(data, indent=2) + '\n'
    if new_text != original:
        if original:
            backup(settings_file)
        settings_file.write_text(new_text, encoding='utf-8')
    return settings_file


def write_codex_default_config(home: Path, base_url: str, alias: str, context: int = 0) -> Path:
    """
    Writes or updates managed block in `<home>/.codex/config.toml`.
    Sets default model to alias (e.g. llamacpp), provider to llmcode,
    approval_policy = 'never', and registers the [model_providers.llmcode] section.
    Preserves user configuration outside the managed block.
    """
    codex_dir = home / '.codex'
    codex_dir.mkdir(parents=True, exist_ok=True)
    cfg = codex_dir / 'config.toml'
    original = cfg.read_text(encoding='utf-8') if cfg.is_file() else ''
    stripped = _strip_block(original).strip()

    block_lines = [
        BLOCK_BEGIN,
        f'model = {_toml_str(alias)}',
        f'model_provider = {_toml_str(CODEX_PROFILE)}',
        'approval_policy = "never"',
    ]
    if context > 0:
        block_lines.append(f'model_context_window = {int(context)}')

    block_lines += [
        '',
        f'[model_providers.{CODEX_PROFILE}]',
        f'name = {_toml_str("llmCode local (" + alias + ")")}',
        f'base_url = {_toml_str(base_url.rstrip("/") + "/v1")}',
        f'env_key = {_toml_str(CODEX_KEY_ENV)}',
        'wire_api = "responses"',
        'stream_idle_timeout_ms = 600000',
    ]

    for name, argv in DEFAULT_MCP_SERVERS.items():
        block_lines += ['', f'[mcp_servers.{_toml_str(name)}]', f'command = {_toml_str(argv[0])}',
                        f'args = [{", ".join(_toml_str(a) for a in argv[1:])}]', 'enabled = false']
    block_lines.append(BLOCK_END)
    block_text = '\n'.join(block_lines)

    new_text = (stripped + '\n\n' + block_text).strip() + '\n' if stripped else (block_text + '\n')
    tomllib.loads(new_text)  # Validate TOML before saving

    if new_text != original:
        if original:
            backup(cfg)
        cfg.write_text(new_text, encoding='utf-8')
    return cfg


def write_codex_profile(home: Path, base_url: str, alias: str, context: int = 0) -> Path:
    """
    Writes dedicated profile `<home>/.codex/llmcode.config.toml`.
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
        'approval_policy = "never"',
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


def setup_all_agent_configs(home: Optional[Path] = None, port: int = 8080, non_interactive: bool = False) -> Dict[str, Path]:
    """
    Configures Claude Code (~/.claude/settings.json) and Codex (~/.codex/config.toml & llmcode.config.toml)
    with model, context size, and bypassPermissions based on config/config.json.
    """
    from src.common.paths import load_config
    user_home = home or Path.home()
    cfg = load_config()
    alias = cfg.get('settings', {}).get('last_alias') or 'llamacpp'
    context = int(cfg.get('settings', {}).get('last_context') or 8192)
    server_port = int(cfg.get('settings', {}).get('server_port') or port)
    base_url = f'http://127.0.0.1:{server_port}'

    results: Dict[str, Path] = {}
    results['claude'] = write_claude_settings(user_home, base_url, alias, context=context)
    results['codex_profile'] = write_codex_profile(user_home, base_url, alias, context=context)
    results['codex_config'] = write_codex_default_config(user_home, base_url, alias, context=context)
    return results
