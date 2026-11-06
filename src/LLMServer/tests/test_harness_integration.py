# -*- coding: utf-8 -*-
"""
Tests for CLI Coding Agent Harness Migration:
  - Harness registry (OpenCode, Claude Code, Codex)
  - Non-destructive Codex profile writer & backups
  - Native vs. LiteLLM endpoint resolution
  - FastAPI harness status and launch routes
"""
import json
import os
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.common.harness import (
    HARNESSES,
    CODEX_PROFILE,
    CODEX_KEY_ENV,
    get_harness,
    claude_env,
    build_launch_command,
    context_warning,
)
from src.common.harness_config import (
    backup,
    write_codex_profile,
    DEFAULT_MCP_SERVERS,
    BLOCK_BEGIN,
    BLOCK_END,
)
from src.common.endpoint import (
    probe_native,
    resolve_base_url,
    proxy_status,
)
from src.LLMServer.app import app


class TestHarnessIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test_harness_"))
        self.home_dir = self.tmp_dir / "user_home"
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_harness_registry_structure(self):
        """Verifies HARNESSES registry contains opencode, claude, codex with correct metadata."""
        self.assertIn('opencode', HARNESSES)
        self.assertIn('claude', HARNESSES)
        self.assertIn('codex', HARNESSES)
        self.assertNotIn('aider', HARNESSES)

        claude = get_harness('claude')
        self.assertEqual(claude.id, 'claude')
        self.assertEqual(claude.label, 'Claude Code')
        self.assertEqual(claude.api_style, 'anthropic')

        codex = get_harness('codex')
        self.assertEqual(codex.id, 'codex')
        self.assertEqual(codex.label, 'Codex')
        self.assertEqual(codex.api_style, 'responses')

        with self.assertRaises(ValueError):
            get_harness('unknown_harness')

    def test_claude_env_and_context_warning(self):
        """Verifies Claude environment variable map and context size advisory."""
        env = claude_env("http://127.0.0.1:8080", "test-model")
        self.assertEqual(env['ANTHROPIC_BASE_URL'], "http://127.0.0.1:8080")
        self.assertEqual(env['ANTHROPIC_MODEL'], "test-model")
        self.assertEqual(env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'], "1")

        # Context warnings
        warn_small = context_warning('claude', 8192)
        self.assertIsNotNone(warn_small)
        self.assertIn(">= 32768 context", warn_small)

        warn_ok = context_warning('claude', 65536)
        self.assertIsNone(warn_ok)

    def test_build_launch_command(self):
        """Verifies terminal command synthesis for Claude and Codex with sessions."""
        key_file = self.tmp_dir / 'api_key'
        key_file.write_text('sk-test-token-1234', encoding='utf-8')

        # Claude Code command
        cmd_claude = build_launch_command(
            'claude',
            '/path/to/claude',
            'llamacpp',
            'http://127.0.0.1:8080',
            restore=True,
            key_file=key_file,
        )
        self.assertIn('ANTHROPIC_BASE_URL', cmd_claude)
        self.assertIn('/path/to/claude --continue', cmd_claude)

        # Codex command with profile and custom session
        cmd_codex = build_launch_command(
            'codex',
            '/path/to/codex',
            'llamacpp',
            'http://127.0.0.1:8080',
            session_id='sess_abc123',
            key_file=key_file,
        )
        self.assertIn(CODEX_KEY_ENV, cmd_codex)
        self.assertIn(f'--profile {CODEX_PROFILE}', cmd_codex)
        self.assertIn('resume sess_abc123', cmd_codex)

    def test_codex_profile_non_destructive_upsert(self):
        """Verifies write_codex_profile writes standalone llmcode.config.toml and cleans legacy config.toml."""
        codex_dir = self.home_dir / '.codex'
        codex_dir.mkdir(parents=True, exist_ok=True)
        config_toml = codex_dir / 'config.toml'

        # Existing user config with custom settings, custom MCP, and a legacy llmcode block
        user_initial_toml = (
            '# User custom settings\n'
            'user_preference = "dark"\n\n'
            f'{BLOCK_BEGIN}\n'
            '[profiles.llmcode]\n'
            'model = "legacy-model"\n'
            f'{BLOCK_END}\n\n'
            '[model_providers.my_cloud]\n'
            'name = "My Cloud Provider"\n'
            'base_url = "https://api.openai.com/v1"\n\n'
            '[mcp_servers.my_custom_mcp]\n'
            'command = "my-mcp"\n'
            'args = ["--serve"]\n'
            'enabled = true\n'
        )
        config_toml.write_text(user_initial_toml, encoding='utf-8')

        # Run write_codex_profile
        written_path = write_codex_profile(self.home_dir, "http://127.0.0.1:8080", "local-qwen", context=32768)
        self.assertEqual(written_path, codex_dir / 'llmcode.config.toml')
        self.assertTrue(written_path.is_file())

        # Verify legacy block was stripped from config.toml and backup was created
        baks = list(codex_dir.glob('config.toml.bak-*'))
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_text(encoding='utf-8'), user_initial_toml)

        cfg_content = config_toml.read_text(encoding='utf-8')
        self.assertNotIn(BLOCK_BEGIN, cfg_content)
        self.assertNotIn('[profiles.llmcode]', cfg_content)
        user_cfg = tomllib.loads(cfg_content)
        self.assertEqual(user_cfg.get('user_preference'), "dark")
        self.assertEqual(user_cfg['model_providers']['my_cloud']['name'], "My Cloud Provider")
        self.assertTrue(user_cfg['mcp_servers']['my_custom_mcp']['enabled'])

        # Parse profile TOML
        profile_content = written_path.read_text(encoding='utf-8')
        self.assertIn(BLOCK_BEGIN, profile_content)
        self.assertIn(BLOCK_END, profile_content)

        data = tomllib.loads(profile_content)
        # Top-level profile keys for Codex v0.167+
        self.assertEqual(data.get('model'), "local-qwen")
        self.assertEqual(data.get('model_provider'), CODEX_PROFILE)
        self.assertEqual(data.get('model_context_window'), 32768)

        # Provider table
        self.assertIn(CODEX_PROFILE, data['model_providers'])
        self.assertEqual(data['model_providers'][CODEX_PROFILE]['base_url'], "http://127.0.0.1:8080/v1")
        self.assertEqual(data['model_providers'][CODEX_PROFILE]['wire_api'], "responses")

        # Default MCP servers registered and disabled
        for mcp in DEFAULT_MCP_SERVERS:
            self.assertIn(mcp, data['mcp_servers'])
            self.assertFalse(data['mcp_servers'][mcp]['enabled'])

        # Idempotency: re-running does not create another backup of config.toml or llmcode.config.toml
        write_codex_profile(self.home_dir, "http://127.0.0.1:8080", "local-qwen", context=32768)
        self.assertEqual(len(list(codex_dir.glob('config.toml.bak-*'))), 1)
        self.assertEqual(len(list(codex_dir.glob('llmcode.config.toml.bak-*'))), 0)

        # Changing profile creates backup of llmcode.config.toml
        write_codex_profile(self.home_dir, "http://127.0.0.1:8080", "updated-qwen", context=65536)
        self.assertEqual(len(list(codex_dir.glob('llmcode.config.toml.bak-*'))), 1)

    def test_endpoint_probe_and_resolution(self):
        """Verifies native multi-turn probe detection and fallback to LiteLLM proxy."""
        # 1. Mock native route 200 OK
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            ok = probe_native('http://127.0.0.1:8080', 'anthropic', 'alias', 'key')
            self.assertTrue(ok)
            # Ensure multi-turn probe payload was sent
            sent_req = mock_urlopen.call_args[0][0]
            body = json.loads(sent_req.data.decode('utf-8'))
            self.assertEqual(body['system'], 'ping')
            self.assertEqual(len(body['messages']), 2)
            self.assertEqual(body['messages'][1]['role'], 'system')

            base_url, mode = resolve_base_url('anthropic', 8080, 'alias', 'key')
            self.assertEqual(base_url, 'http://127.0.0.1:8080')
            self.assertEqual(mode, 'native')

        # 2. Mock 500 Server Error (e.g. Jinja Exception) -> triggers proxy fallback
        with patch('src.common.endpoint.probe_native', return_value=False), \
             patch('src.common.endpoint.ensure_litellm_proxy', return_value='http://127.0.0.1:8180') as mock_proxy:
            base_url, mode = resolve_base_url('anthropic', 8080, 'alias', 'key')
            self.assertEqual(base_url, 'http://127.0.0.1:8180')
            self.assertEqual(mode, 'proxy')
            mock_proxy.assert_called_once_with('http://127.0.0.1:8080', 8080, 'alias', 'key')
            self.assertEqual(mode, 'proxy')
            mock_proxy.assert_called_once_with('http://127.0.0.1:8080', 8080, 'alias', 'key')

    def test_fastapi_harness_status_endpoints(self):
        """Verifies GET /api/harness/status, GET /api/harness/{id}/status, and legacy opencode route."""
        # All harnesses status
        resp = self.client.get('/api/harness/status')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('harnesses', data)
        self.assertIn('opencode', data['harnesses'])
        self.assertIn('claude', data['harnesses'])
        self.assertIn('codex', data['harnesses'])
        self.assertIn('proxy', data)

        # Single harness status
        r_claude = self.client.get('/api/harness/claude/status')
        self.assertEqual(r_claude.status_code, 200)
        self.assertEqual(r_claude.json()['label'], 'Claude Code')

        # Legacy opencode endpoint compatibility
        r_oc = self.client.get('/api/opencode/status')
        self.assertEqual(r_oc.status_code, 200)
        self.assertEqual(r_oc.json()['label'], 'OpenCode')

        # Invalid harness returns 400
        r_invalid = self.client.get('/api/harness/nonexistent/status')
        self.assertEqual(r_invalid.status_code, 400)

        # Retired aider status returns 404 (route removed)
        r_aider = self.client.get('/api/aider/status')
        self.assertEqual(r_aider.status_code, 404)


if __name__ == '__main__':
    unittest.main()
