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

# Shared MCP catalogue with 2-tier categorization (Tier 1: Zero-config local; Tier 2: Cloud / API-key)
DEFAULT_MCP_CATALOG: Dict[str, Dict[str, Any]] = {
    # Tier 1: Zero-config local utilities (uvx / npx)
    'fetch': {
        'command': 'uvx',
        'args': ['mcp-server-fetch'],
        'tier': 1,
        'description': 'Web fetch and HTML-to-Markdown conversion',
    },
    'context7': {
        'command': 'npx',
        'args': ['-y', '@upstash/context7-mcp'],
        'tier': 1,
        'description': 'Live library documentation and API reference search',
    },
    'sequential-thinking': {
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-sequential-thinking'],
        'tier': 1,
        'description': 'Dynamic step-by-step reasoning and hypothesis branching',
    },
    'playwright': {
        'command': 'npx',
        'args': ['-y', '@playwright/mcp@latest'],
        'tier': 1,
        'description': 'Browser automation, DOM interaction, and screenshots',
    },
    'git': {
        'command': 'uvx',
        'args': ['mcp-server-git'],
        'tier': 1,
        'description': 'Git repository diffs, log inspection, and history',
    },
    'memory': {
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-memory'],
        'tier': 1,
        'description': 'Persistent graph-based agent memory across sessions',
    },
    'filesystem': {
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-filesystem', str(Path.home())],
        'tier': 1,
        'description': 'Explicit local filesystem directory exploration',
    },
    'sqlite': {
        'command': 'uvx',
        'args': ['mcp-server-sqlite'],
        'tier': 1,
        'description': 'Local SQLite database queries and schema inspection',
    },
    # Tier 2: Cloud / API-key required utilities
    'github': {
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-github'],
        'tier': 2,
        'env_var': 'GITHUB_PERSONAL_ACCESS_TOKEN',
        'description': 'GitHub API for repos, pull requests, issues, and search',
    },
    'brave-search': {
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-brave-search'],
        'tier': 2,
        'env_var': 'BRAVE_API_KEY',
        'description': 'Brave web and news search engine',
    },
    'tavily': {
        'command': 'npx',
        'args': ['-y', 'tavily-mcp'],
        'tier': 2,
        'env_var': 'TAVILY_API_KEY',
        'description': 'Tavily AI-optimized research and search engine',
    },
    'firecrawl': {
        'command': 'npx',
        'args': ['-y', 'firecrawl-mcp'],
        'tier': 2,
        'env_var': 'FIRECRAWL_API_KEY',
        'description': 'Firecrawl deep web crawling and markdown extraction',
    },
}

# Backward-compatible command-args mapping
DEFAULT_MCP_SERVERS: Dict[str, List[str]] = {
    name: [spec['command']] + list(spec['args']) for name, spec in DEFAULT_MCP_CATALOG.items()
}

# Recommended default local servers for zero-config workflows
RECOMMENDED_MCP_SERVERS: List[str] = [
    'fetch',
    'sequential-thinking',
    'context7',
    'memory',
    'playwright',
    'filesystem',
]


def get_configured_mcp_servers() -> Dict[str, Dict[str, Any]]:
    """Returns actively enabled MCP servers from config/config.json, adhering to strict opt-in with fallback."""
    try:
        from src.common.paths import load_config
        cfg = load_config()
        mcp_data = cfg.get('mcp', {})
        enabled = mcp_data.get('enabled_servers', [])
        servers = mcp_data.get('servers', {})
        if enabled:
            return {k: v for k, v in servers.items() if k in enabled}
        res: Dict[str, Dict[str, Any]] = {}
        for k in RECOMMENDED_MCP_SERVERS:
            if k in DEFAULT_MCP_CATALOG:
                spec = DEFAULT_MCP_CATALOG[k]
                res[k] = {
                    'command': spec['command'],
                    'args': list(spec['args']),
                }
        return res
    except Exception:
        return {}


def backup(path: Path) -> Optional[Path]:
    """Copies `path` to `path.bak-YYYYmmdd-HHMMSS` when it exists; returns the backup path."""
    if not path.is_file():
        return None
    dest = path.with_name(f'{path.name}.bak-{datetime.now().strftime("%Y%m%d-%H%M%S")}')
    shutil.copy2(path, dest)
    return dest


def _strip_block(text: str) -> str:
    if BLOCK_BEGIN not in text:
        return text
    pattern = re.compile(rf'\n?{re.escape(BLOCK_BEGIN)}.*?{re.escape(BLOCK_END)}\n?', re.S)
    stripped = pattern.sub('\n', text).strip()
    return (stripped + '\n') if stripped else ''


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
    Preserves other user-defined settings, creates timestamped backup, and writes
    dedicated `<home>/.claude/llmcode.settings.json` for `--settings` local launches.
    """
    claude_dir = home / '.claude'
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_file = claude_dir / 'settings.json'
    llmcode_settings_file = claude_dir / 'llmcode.settings.json'

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

    # Write dedicated llmcode.settings.json for `claude --settings`
    local_payload = {
        'defaultMode': 'bypassPermissions',
        'permissions': {'defaultMode': 'bypassPermissions'},
        'model': alias,
        'env': {
            'ANTHROPIC_BASE_URL': base_url,
            'ANTHROPIC_MODEL': alias,
            'ANTHROPIC_SMALL_FAST_MODEL': alias,
            'ANTHROPIC_DEFAULT_OPUS_MODEL': alias,
            'ANTHROPIC_DEFAULT_SONNET_MODEL': alias,
            'ANTHROPIC_DEFAULT_HAIKU_MODEL': alias,
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
            'DISABLE_TELEMETRY': '1',
            'API_TIMEOUT_MS': '600000',
        },
    }
    llmcode_settings_file.write_text(json.dumps(local_payload, indent=2) + '\n', encoding='utf-8')
    return settings_file


def write_claude_mcp_config(home: Path, mcp_servers: Optional[Dict[str, Dict[str, Any]]] = None) -> Path:
    """
    Writes or updates user-scope MCP servers in `<home>/.claude.json`.
    Strict opt-in: only actively selected MCP servers are written.
    """
    claude_json = home / '.claude.json'
    data: Dict[str, Any] = {}
    original = ''
    if claude_json.is_file():
        original = claude_json.read_text(encoding='utf-8')
        try:
            parsed = json.loads(original)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            pass

    selected = mcp_servers if mcp_servers is not None else get_configured_mcp_servers()
    mcp_map = data.setdefault('mcpServers', {})

    for name, spec in selected.items():
        cmd = spec.get('command') or ''
        args = spec.get('args') or []
        env = spec.get('env') or {}
        entry: Dict[str, Any] = {
            'type': spec.get('type') or 'stdio',
            'command': cmd,
            'args': list(args),
            'env': env,
        }
        mcp_map[name] = entry

    for name in DEFAULT_MCP_CATALOG:
        if name not in selected and name in mcp_map:
            mcp_map.pop(name, None)

    new_text = json.dumps(data, indent=2) + '\n'
    if new_text != original:
        if original:
            backup(claude_json)
        claude_json.write_text(new_text, encoding='utf-8')
    return claude_json


def write_codex_profile(
    home: Path,
    base_url: str,
    alias: str,
    context: int = 0,
    mcp_servers: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Path:
    """
    Writes dedicated profile `<home>/.codex/llmcode.config.toml`.
    Codex v0.167+ overlays `<home>/.codex/<profile>.config.toml` on top of `config.toml`.
    Adheres to strict opt-in and checks base config.toml to avoid duplicate MCP table declarations.
    """
    codex_dir = home / '.codex'
    codex_dir.mkdir(parents=True, exist_ok=True)
    cleanup_legacy_codex_config(home)

    base_cfg = codex_dir / 'config.toml'
    existing_base_mcps = set()
    if base_cfg.is_file():
        try:
            base_parsed = tomllib.loads(base_cfg.read_text(encoding='utf-8'))
            existing_base_mcps = set(base_parsed.get('mcp_servers', {}).keys())
        except Exception:
            pass

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

    selected = mcp_servers if mcp_servers is not None else get_configured_mcp_servers()
    if selected:
        for name, spec in selected.items():
            if name in existing_base_mcps:
                continue
            cmd = spec.get('command') or ''
            args = spec.get('args') or []
            env = spec.get('env') or {}
            lines += ['', f'[mcp_servers.{_toml_str(name)}]', f'command = {_toml_str(cmd)}',
                      f'args = [{", ".join(_toml_str(a) for a in args)}]']
            if env:
                env_pairs = ', '.join(f'{_toml_str(k)} = {_toml_str(v)}' for k, v in env.items())
                lines.append(f'env = {{{env_pairs}}}')
            lines.append('enabled = true')
    else:
        for name, argv in DEFAULT_MCP_SERVERS.items():
            if name in existing_base_mcps:
                continue
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


def write_codex_default_config(
    home: Path,
    base_url: str,
    alias: str,
    context: int = 0,
    mcp_servers: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Path:
    """
    Writes or updates managed block in `<home>/.codex/config.toml` safely and synchronizes profile.
    Sets default model to alias (e.g. llamacpp), provider to llmcode, approval_policy = 'never',
    and registers the [model_providers.llmcode] section without duplicate MCP table collisions.
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
    block_lines.append(BLOCK_END)
    block_text = '\n'.join(block_lines)

    new_text = (stripped + '\n\n' + block_text).strip() + '\n' if stripped else (block_text + '\n')
    tomllib.loads(new_text)  # Validate TOML before saving

    if new_text != original:
        if original:
            backup(cfg)
        cfg.write_text(new_text, encoding='utf-8')

    write_codex_profile(home, base_url, alias, context=context, mcp_servers=mcp_servers)
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
    results['claude_mcp'] = write_claude_mcp_config(user_home)
    results['codex_profile'] = write_codex_profile(user_home, base_url, alias, context=context)
    results['codex_config'] = write_codex_default_config(user_home, base_url, alias, context=context)
    return results
