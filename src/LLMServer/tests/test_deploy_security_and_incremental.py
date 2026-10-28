# -*- coding: utf-8 -*-
"""
Tests for security hardening, per-deploy API key rotation, file-based credential delivery,
command log masking, LAN exposure controls, and pull-aware incremental update analysis.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.paths import (
    API_KEY_FILE,
    AGENT_ENV_FILE,
    CONFIG_FILE,
    load_config,
    save_config,
    write_secret,
)
from src.LLMServer.app import (
    Deploy,
    clear_api_key,
    get_current_api_key,
    load_settings,
    mask_command_args,
    rotate_api_key,
    write_opencode_config,
)
from src.build.deploy_state import (
    diff_components,
    load_deploy_state,
    save_deploy_state,
)
from src.build.setup_binaries import render_desktop_entry


class TestDeploySecurityAndKeyRotation(unittest.TestCase):
    def setUp(self):
        clear_api_key()

    def tearDown(self):
        clear_api_key()

    def test_write_secret_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            secret_file = Path(tmpdir) / 'test_secret'
            write_secret(secret_file, 'super-secret-token')
            self.assertTrue(secret_file.is_file())
            self.assertEqual(secret_file.read_text(encoding='utf-8'), 'super-secret-token')
            if os.name != 'nt':
                mode = stat.S_IMODE(secret_file.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_key_rotation_different_keys_and_files(self):
        key1 = rotate_api_key()
        self.assertTrue(key1.startswith('sk-'))
        self.assertEqual(get_current_api_key(), key1)
        self.assertTrue(API_KEY_FILE.is_file())
        self.assertTrue(AGENT_ENV_FILE.is_file())
        self.assertEqual(API_KEY_FILE.read_text(encoding='utf-8').strip(), key1)
        agent_env = AGENT_ENV_FILE.read_text(encoding='utf-8')
        self.assertIn(f"OPENAI_API_KEY={key1}", agent_env)
        self.assertIn("OPENAI_API_BASE=http://127.0.0.1:8080/v1", agent_env)

        # Rotate again: verify fresh key replaces previous
        key2 = rotate_api_key()
        self.assertNotEqual(key1, key2)
        self.assertEqual(get_current_api_key(), key2)
        self.assertEqual(API_KEY_FILE.read_text(encoding='utf-8').strip(), key2)

        # Clear key: deletes files from disk
        clear_api_key()
        self.assertIsNone(get_current_api_key())
        self.assertFalse(API_KEY_FILE.exists())
        self.assertFalse(AGENT_ENV_FILE.exists())

    def test_legacy_api_key_purged_from_settings(self):
        cfg = load_config()
        cfg.setdefault('settings', {})['api_key'] = 'sk-legacy-test-key'
        save_config(cfg)

        settings = load_settings()
        self.assertNotIn('api_key', settings)

    def test_mask_command_args(self):
        args = [
            'llama-server',
            '--api-key', 'sk-super-secret-12345',
            '--api-key-file', '/path/to/config/api_key',
            '--password', 'secret-password-xyz',
            '--ctx-size', '8192',
        ]
        masked = mask_command_args(args)
        self.assertEqual(masked[0], 'llama-server')
        self.assertEqual(masked[1], '--api-key')
        self.assertEqual(masked[2], '***')
        self.assertEqual(masked[3], '--api-key-file')
        self.assertEqual(masked[4], '***')
        self.assertEqual(masked[5], '--password')
        self.assertEqual(masked[6], '***')
        self.assertEqual(masked[7], '--ctx-size')
        self.assertEqual(masked[8], '8192')
        self.assertNotIn('sk-super-secret-12345', masked)
        self.assertNotIn('secret-password-xyz', masked)


class TestAgentConfigurationAndGitHygiene(unittest.TestCase):
    def test_opencode_config_uses_file_and_ignores_gitignore(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proj = Path(tmpdir)
            (proj / '.git').mkdir()
            (proj / '.gitignore').write_text("node_modules/\n", encoding='utf-8')

            cfg_file = write_opencode_config(proj, 'llamacpp', 8080)
            self.assertTrue(cfg_file.is_file())
            data = json.loads(cfg_file.read_text(encoding='utf-8'))

            # Check apiKey uses file: placeholder
            api_key_val = data['provider']['llmcode']['options']['apiKey']
            self.assertTrue(api_key_val.startswith('{file:'))
            self.assertTrue(api_key_val.endswith('/api_key}'))
            self.assertNotIn('sk-', api_key_val)

            # Check reasoning boolean true without effort option
            model_cfg = data['provider']['llmcode']['models']['llamacpp']
            self.assertTrue(model_cfg.get('reasoning'))
            self.assertNotIn('reasoning_options', model_cfg)

            # Verify opencode.json was added to .gitignore
            gitignore_content = (proj / '.gitignore').read_text(encoding='utf-8')
            self.assertIn('opencode.json', gitignore_content)

    def test_render_desktop_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            in_file = bin_dir / 'llmCode.desktop.in'
            in_file.write_text("Exec=@LLMCODE_DIR@/launch.sh\nIcon=@LLMCODE_DIR@/bin/llmCode.svg\n", encoding='utf-8')

            out_file = render_desktop_entry(root)
            self.assertIsNotNone(out_file)
            self.assertTrue(out_file.is_file())
            content = out_file.read_text(encoding='utf-8')
            self.assertIn(f"Exec={root.resolve()}/launch.sh", content)
            self.assertIn(f"Icon={root.resolve()}/bin/llmCode.svg", content)
            self.assertNotIn("@LLMCODE_DIR@", content)


class TestIncrementalDeployState(unittest.TestCase):
    def test_diff_components_with_no_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            res = diff_components(root)
            self.assertFalse(res['has_prior_state'])
            self.assertEqual(res['steps'], ['full'])

    def test_diff_components_file_hashing_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg_dir = root / 'config'
            cfg_dir.mkdir(parents=True)
            server_dir = root / 'src' / 'LLMServer'
            server_dir.mkdir(parents=True)
            req_file = server_dir / 'requirements.txt'
            req_file.write_text("fastapi\nuvicorn\n", encoding='utf-8')

            # Save initial state
            save_deploy_state(root)

            # Modify requirements.txt
            req_file.write_text("fastapi\nuvicorn\npydantic\n", encoding='utf-8')

            # Evaluate diff
            res = diff_components(root)
            self.assertTrue(res['has_prior_state'])
            self.assertIn('pip', res['steps'])
            self.assertNotIn('engines', res['steps'])


if __name__ == '__main__':
    unittest.main()
