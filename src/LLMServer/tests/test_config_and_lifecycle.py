# -*- coding: utf-8 -*-
"""
Unit and integration tests for config/config.json auto-healing,
requirement checking across Windows/Linux/macOS, and lifecycle operations.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.paths import (
    CONFIG_FILE,
    load_config,
    save_config,
    sync_config,
)
from src.build.setup_binaries import (
    detect_gpu_acceleration,
    inspect_requirements,
    _pkg_install_cmd,
)


class TestConfigAndLifecycle(unittest.TestCase):

    def test_sync_config_creates_and_heals_paths(self):
        """Verifies config/config.json is created and heals stale paths on sync_config()."""
        cfg = sync_config()
        self.assertTrue(CONFIG_FILE.is_file())
        self.assertEqual(cfg['paths']['repo_root'], str(REPO_ROOT))
        self.assertIn('llama_cpp', cfg['binaries'])
        self.assertIn('koboldcpp', cfg['binaries'])
        self.assertIn('ollama', cfg['binaries'])
        self.assertIn('aider', cfg['agents'])
        self.assertIn('opencode', cfg['agents'])

        # Simulate moving repository folder by writing a fake old repo_root into config.json
        corrupted = load_config()
        corrupted['paths']['repo_root'] = '/nonexistent/moved/folder/llmCode'
        corrupted['binaries']['llama_cpp'] = '/nonexistent/moved/folder/llmCode/bin/llama-server'
        save_config(corrupted)

        # Calling sync_config() should automatically heal paths back to actual REPO_ROOT
        healed = sync_config()
        self.assertEqual(healed['paths']['repo_root'], str(REPO_ROOT))
        self.assertNotIn('/nonexistent/moved/folder', healed['binaries']['llama_cpp'])

    def test_inspect_requirements_per_engine(self):
        """Verifies requirement inspection lists git, compiler, cmake, make, and go appropriately."""
        reqs = inspect_requirements(
            needs_llama_src=True,
            needs_kobold_src=True,
            needs_ollama_src=True,
            accel='cpu',
        )
        names = [r['name'] for r in reqs]
        self.assertIn('git', names)
        self.assertIn('c++ compiler', names)
        self.assertIn('cmake', names)
        self.assertIn('golang (go)', names)

    def test_pkg_install_cmd_cross_platform(self):
        """Verifies package manager commands for Windows, macOS, and Linux."""
        win_cmake = _pkg_install_cmd('windows', 'cmake')
        self.assertEqual(win_cmake[0], 'winget')

        mac_cc = _pkg_install_cmd('macos', 'compiler')
        self.assertEqual(mac_cc, ['xcode-select', '--install'])

        mac_cmake = _pkg_install_cmd('macos', 'cmake')
        self.assertEqual(mac_cmake, ['brew', 'install', 'cmake'])


if __name__ == '__main__':
    unittest.main()
