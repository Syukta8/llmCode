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
    write_claude_settings,
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
        self.assertEqual(env['ENABLE_TOOL_SEARCH'], "false")
        self.assertEqual(env['CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS'], "1")

        # write_claude_settings preserves both flags in settings.json and llmcode.settings.json
        settings_path = write_claude_settings(self.home_dir, "http://127.0.0.1:8080", "test-model", context=65536)
        local_settings_path = self.home_dir / '.claude' / 'llmcode.settings.json'
        for p in (settings_path, local_settings_path):
            parsed = json.loads(p.read_text(encoding='utf-8'))
            self.assertEqual(parsed['env']['ENABLE_TOOL_SEARCH'], "false")
            self.assertEqual(parsed['env']['CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS'], "1")

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

    def test_write_opencode_config_v2_schema(self):
        """Verifies write_opencode_config emits clean v2 schema without legacy redundant keys."""
        import src.LLMServer.app as app_mod

        cfg_path = self.home_dir / '.config' / 'opencode' / 'opencode.jsonc'
        with patch.object(app_mod, 'GLOBAL_OPENCODE_CONFIG', cfg_path), \
             patch.object(app_mod, 'HOME', self.home_dir):
            app_mod.write_opencode_config(self.tmp_dir, 'llamacpp', 8080, context_limit=65536, mcp_servers={})

        data = json.loads(cfg_path.read_text(encoding='utf-8'))
        self.assertEqual(data['model'], 'llmcode/llamacpp')
        self.assertNotIn('provider', data)
        self.assertNotIn('permission', data)
        provider = data['providers']['llmcode']
        self.assertNotIn('npm', provider)
        self.assertNotIn('options', provider)
        model = provider['models']['llamacpp']
        self.assertNotIn('reasoning', model)
        self.assertTrue(model['capabilities']['reasoning']['supported'])
        self.assertIsInstance(model['variants'], list)
        self.assertIn('repowise', data['mcp']['servers'])
        self.assertIn('rea', data['mcp']['servers'])
        for srv in data['mcp']['servers'].values():
            self.assertNotIn('enabled', srv)
            self.assertTrue(srv['disabled'])

    def test_parse_harness_selection_and_selected_agents(self):
        """Verifies individual, multi-select, all, and skip-all harness selection parsing."""
        from src.build.setup_binaries import parse_harness_selection
        from src.common.paths import get_selected_agents

        self.assertEqual(parse_harness_selection('a'), ['opencode', 'claude', 'codex'])
        self.assertEqual(parse_harness_selection('ALL'), ['opencode', 'claude', 'codex'])
        self.assertEqual(parse_harness_selection('s'), [])
        self.assertEqual(parse_harness_selection('skip all'), [])
        self.assertEqual(parse_harness_selection('1'), ['opencode'])
        self.assertEqual(parse_harness_selection('2'), ['claude'])
        self.assertEqual(parse_harness_selection('3'), ['codex'])
        self.assertEqual(parse_harness_selection('codex, opencode'), ['opencode', 'codex'])
        self.assertEqual(parse_harness_selection('2 3'), ['claude', 'codex'])
        self.assertIsNone(parse_harness_selection('invalid'))

        self.assertEqual(get_selected_agents({'selected_agents': []}), [])
        self.assertEqual(get_selected_agents({'selected_agents': ['claude']}), ['claude'])
        self.assertEqual(
            get_selected_agents({'agents': {'opencode_installed': True, 'claude_installed': False, 'codex_installed': False}}),
            ['opencode'],
        )

    def test_claude_apikey_helper_and_codex_shim(self):
        """Verifies Claude apiKeyHelper cleanup/generation, Codex shim, and scoped setup_all_agent_configs."""
        from src.common.harness_config import write_codex_shim, setup_all_agent_configs

        key_file = self.tmp_dir / 'api_key'
        key_file.write_text('sk-local-test', encoding='utf-8')

        claude_dir = self.home_dir / '.claude'
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / 'settings.json').write_text(
            json.dumps({'env': {'ANTHROPIC_API_KEY': '{file:D:/llmCode/config/api_key}'}}),
            encoding='utf-8',
        )
        settings_path = write_claude_settings(self.home_dir, 'http://127.0.0.1:8180', 'llamacpp', context=65536, key_file=key_file)
        parsed = json.loads(settings_path.read_text(encoding='utf-8'))
        self.assertIn('apiKeyHelper', parsed)
        self.assertIn(str(key_file.resolve()), parsed['apiKeyHelper'])
        self.assertNotIn('ANTHROPIC_API_KEY', parsed['env'])
        self.assertEqual(parsed['env']['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:8180')

        shims = write_codex_shim(self.home_dir, '/path/to/codex', key_file=key_file)
        self.assertEqual(len(shims), 1)
        self.assertTrue(shims[0].is_file())
        shim_text = shims[0].read_text(encoding='utf-8')
        self.assertIn(CODEX_KEY_ENV, shim_text)
        self.assertIn(f'--profile {CODEX_PROFILE}', shim_text)

        isolated_home = self.tmp_dir / 'opencode_only_home'
        isolated_home.mkdir(parents=True, exist_ok=True)
        res = setup_all_agent_configs(home=isolated_home, selected_agents=['opencode'])
        self.assertEqual(res, {})
        self.assertFalse((isolated_home / '.claude').exists())
        self.assertFalse((isolated_home / '.codex').exists())

    def test_sync_external_harnesses_updates_proxy_base_url(self):
        """Verifies _sync_external_harnesses starts proxy directly and syncs proxy base_url into Claude settings."""
        import src.LLMServer.app as app_mod

        fake_cfg = {
            'selected_agents': ['claude'],
            'agents': {'claude_installed': True, 'codex_installed': False},
        }
        with patch.object(app_mod, 'HOME', self.home_dir), \
             patch.object(app_mod, 'load_config', return_value=fake_cfg), \
             patch.object(app_mod, 'ensure_litellm_proxy', return_value='http://127.0.0.1:8180') as mock_proxy:
            app_mod._sync_external_harnesses('llamacpp', 65536, 'sk-test')

        mock_proxy.assert_called_once_with(f'http://127.0.0.1:{app_mod.PORT}', app_mod.PORT, 'llamacpp', 'sk-test')
        parsed = json.loads((self.home_dir / '.claude' / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(parsed['env']['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:8180')
        self.assertIn('apiKeyHelper', parsed)

    def test_litellm_proxy_sets_utf8_env(self):
        """Verifies ensure_litellm_proxy exports UTF-8 env vars so Windows cp1252 does not crash banner output."""
        import src.common.endpoint as ep

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        fake_exe = self.tmp_dir / 'litellm.exe'
        fake_exe.write_text('', encoding='utf-8')
        fake_yaml = self.tmp_dir / 'litellm_proxy.yaml'
        fake_log = self.tmp_dir / 'litellm-proxy.log'

        with patch.object(ep, '_litellm_bin', return_value=fake_exe), \
             patch.object(ep, 'PROXY_CONFIG_FILE', fake_yaml), \
             patch.object(ep, 'PROXY_LOG_FILE', fake_log), \
             patch.object(ep, '_proxy', {'proc': None, 'signature': None, 'port': None}), \
             patch.object(ep, '_wait_ready'), \
             patch('subprocess.Popen', return_value=mock_proc) as mock_popen:
            url = ep.ensure_litellm_proxy('http://127.0.0.1:8080', 8080, 'llamacpp', 'sk-test')

        self.assertEqual(url, 'http://127.0.0.1:8180')
        popen_env = mock_popen.call_args.kwargs['env']
        self.assertEqual(popen_env['PYTHONUTF8'], '1')
        self.assertEqual(popen_env['PYTHONIOENCODING'], 'utf-8')
        self.assertIn(str(ep.REPO_ROOT), popen_env['PYTHONPATH'])
        yaml_text = fake_yaml.read_text(encoding='utf-8')
        self.assertIn('src.common.litellm_hooks.proxy_effort_hook', yaml_text)

    def test_reasoning_effort_normalizer_maps_high_to_xhigh(self):
        """Verifies LiteLLM pre-call normalizer maps high/max effort and large thinking budgets to xhigh."""
        from src.common.litellm_hooks import normalize_reasoning_effort_payload

        self.assertEqual(normalize_reasoning_effort_payload({'reasoning_effort': 'high'})['reasoning_effort'], 'xhigh')
        self.assertEqual(normalize_reasoning_effort_payload({'reasoning_effort': 'max'})['reasoning_effort'], 'xhigh')
        self.assertEqual(normalize_reasoning_effort_payload({'reasoning_effort': 'xhigh'})['reasoning_effort'], 'xhigh')
        self.assertEqual(normalize_reasoning_effort_payload({'reasoning_effort': 'medium'})['reasoning_effort'], 'medium')
        self.assertEqual(normalize_reasoning_effort_payload({'reasoning_effort': 'low'})['reasoning_effort'], 'low')

        oc_high = normalize_reasoning_effort_payload({'output_config': {'effort': 'high'}})
        self.assertEqual(oc_high['output_config']['effort'], 'xhigh')
        oc_med = normalize_reasoning_effort_payload({'output_config': {'effort': 'medium'}})
        self.assertEqual(oc_med['output_config']['effort'], 'medium')

        th_high = normalize_reasoning_effort_payload({'thinking': {'type': 'enabled', 'budget_tokens': 16000}})
        self.assertEqual(th_high['reasoning_effort'], 'xhigh')
        th_low = normalize_reasoning_effort_payload({'thinking': {'type': 'enabled', 'budget_tokens': 1024}})
        self.assertNotIn('reasoning_effort', th_low)


if __name__ == '__main__':
    unittest.main()
