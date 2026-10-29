# -*- coding: utf-8 -*-
"""
Unit and integration tests for config/hardware.json scanning, config/config.json
auto-healing and hardware tuning, VS Code Continue & OpenCode standard alias
configuration ('llamacpp', 'koboldcpp', 'ollama'), and full --uninstall.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.paths import (
    CONFIG_FILE,
    HARDWARE_FILE,
    get_standard_alias,
    load_config,
    load_hardware,
    save_config,
    sync_config,
)
from src.build.setup_binaries import (
    _pkg_install_cmd,
    inspect_requirements,
    scan_and_save_hardware,
    setup_continue_config,
    uninstall_all,
)
from src.common.rag import (
    generate_rag_context_file,
    index_workspace,
    retrieve_chunks,
)
from src.LLMServer.app import (
    Deploy,
    build_context_extension_flags,
    get_gguf_architecture,
    write_opencode_config,
)


class TestConfigAndLifecycle(unittest.TestCase):

    def test_scan_and_save_hardware_json(self):
        """Verifies scan_and_save_hardware() creates config/hardware.json with CPU/RAM/GPU/packages and build/runtime tuning."""
        hw = scan_and_save_hardware()
        self.assertTrue(HARDWARE_FILE.is_file())
        loaded = load_hardware()
        self.assertIn('cpu', loaded)
        self.assertIn('ram', loaded)
        self.assertIn('gpu', loaded)
        self.assertIn('packages', loaded)
        self.assertIn('build_config', loaded)
        self.assertIn('runtime_defaults', loaded)
        self.assertIn('layers', loaded['runtime_defaults'])
        self.assertIn('context', loaded['runtime_defaults'])

    def test_sync_config_creates_heals_and_uses_standard_alias(self):
        """Verifies config/config.json is created, uses standard alias 'llamacpp', and heals stale paths."""
        scan_and_save_hardware()
        cfg = sync_config(apply_hardware_defaults=True)
        self.assertTrue(CONFIG_FILE.is_file())
        self.assertEqual(cfg['paths']['repo_root'], str(REPO_ROOT))
        self.assertEqual(cfg['settings']['last_alias'], 'llamacpp')
        self.assertEqual(get_standard_alias('llama.cpp'), 'llamacpp')
        self.assertEqual(get_standard_alias('koboldcpp'), 'koboldcpp')
        self.assertEqual(get_standard_alias('ollama'), 'ollama')

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

    def test_deploy_high_context_and_standard_alias(self):
        """Verifies Deploy model defaults to 'llamacpp' alias and accepts 265,356 context."""
        d = Deploy(path='test.gguf', context=265356, layers=32, threads=24)
        self.assertEqual(d.alias, 'llamacpp')
        self.assertEqual(d.context, 265356)

    def test_continue_and_opencode_standard_alias_configs(self):
        """Verifies Continue (config.yaml & config.json) and OpenCode (opencode.json) use standard aliases."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)

            # 1. Test Continue config.yaml
            yaml_file = tmp_dir / 'config.yaml'
            yaml_file.write_text("name: Local Config\nversion: 1.0.0\nmodels:\n", encoding='utf-8')
            updated = setup_continue_config(continue_dir=tmp_dir, port=8080, non_interactive=True)
            self.assertEqual(updated, yaml_file)
            yaml_text = yaml_file.read_text(encoding='utf-8')
            self.assertIn('model: llamacpp', yaml_text)
            self.assertIn('model: koboldcpp', yaml_text)
            self.assertIn('model: ollama', yaml_text)

            # 2. Test OpenCode opencode.json
            oc_file = write_opencode_config(tmp_dir, 'koboldcpp', 8080)
            oc_data = json.loads(oc_file.read_text(encoding='utf-8'))
            self.assertEqual(oc_data['model'], 'llmcode/koboldcpp')
            self.assertIn('koboldcpp', oc_data['provider']['llmcode']['models'])

    def test_uninstall_all_isolated_fixture(self):
        """Verifies uninstall_all() removes .venv, engine sources, binaries, models, configs, and logs."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            for sub in (
                'src/LLMServer/.venv',
                'src/llama.cpp',
                'src/koboldcpp',
                'src/ollama',
                'bin/windows',
                'config',
                'logs',
                'models',
            ):
                p = fake_root / sub
                p.mkdir(parents=True, exist_ok=True)
                (p / 'dummy.txt').write_text('data', encoding='utf-8')
            (fake_root / 'bin' / 'llmCode.svg').write_text('<svg/>', encoding='utf-8')

            ok = uninstall_all(repo_root=fake_root, non_interactive=True)
            self.assertTrue(ok)
            self.assertFalse((fake_root / 'src' / 'LLMServer' / '.venv').exists())
            self.assertFalse((fake_root / 'src' / 'llama.cpp').exists())
            self.assertFalse((fake_root / 'models' / 'dummy.txt').exists())
            self.assertTrue((fake_root / 'bin' / 'llmCode.svg').is_file())

    def test_rope_and_yarn_context_extension_flags(self):
        """Verifies RoPE and YaRN CLI flags for llama.cpp and koboldcpp."""
        self.assertEqual(build_context_extension_flags('llama.cpp', 'none'), [])
        self.assertEqual(
            build_context_extension_flags('llama.cpp', 'linear_2x'),
            ['--rope-scaling', 'linear', '--rope-scale', '2'],
        )
        self.assertEqual(
            build_context_extension_flags('llama.cpp', 'yarn_4x'),
            ['--rope-scaling', 'yarn', '--rope-scale', '4', '--yarn-ext-factor', '-1.0'],
        )
        self.assertEqual(
            build_context_extension_flags('koboldcpp', 'yarn_8x'),
            ['--ropeconfig', '0.125', '10000'],
        )

    def test_workspace_rag_indexer_and_retrieval(self):
        """Verifies local workspace chunk indexing, BM25 retrieval, and rag_context.md injection."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'sample_repo'
            ws.mkdir()
            (ws / 'auth.py').write_text(
                'def verify_jwt_token(token: str) -> bool:\n'
                '    """Validates HMAC-SHA256 JWT token signature."""\n'
                '    return token.startswith("eyJ")\n',
                encoding='utf-8',
            )
            (ws / 'math_utils.py').write_text(
                'def fibonacci(n: int) -> int:\n'
                '    return n if n <= 1 else fibonacci(n - 1) + fibonacci(n - 2)\n',
                encoding='utf-8',
            )

            idx = index_workspace(ws)
            self.assertEqual(idx['file_count'], 2)
            self.assertGreaterEqual(idx['chunk_count'], 2)

            top = retrieve_chunks('verify_jwt_token signature', top_k=2, project_dir=ws)
            self.assertTrue(len(top) >= 1)
            self.assertEqual(top[0]['file'], 'auth.py')

            out_md = generate_rag_context_file(ws, query='JWT token', top_k=2)
            self.assertTrue(out_md.is_file())
            self.assertIn('verify_jwt_token', out_md.read_text(encoding='utf-8'))

            oc_file = write_opencode_config(ws, 'llamacpp', 8080, rag_context_file=out_md)
            oc_data = json.loads(oc_file.read_text(encoding='utf-8'))
            self.assertIn(str(out_md.resolve()), oc_data.get('instructions', []))

    def test_opencode_config_context_limit(self):
        """Verifies write_opencode_config writes explicit context and output limits."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'project'
            ws.mkdir()
            oc_file = write_opencode_config(ws, 'llamacpp', 8080, context_limit=524288)
            oc_data = json.loads(oc_file.read_text(encoding='utf-8'))
            model_info = oc_data['provider']['llmcode']['models']['llamacpp']
            self.assertIn('limit', model_info)
            self.assertEqual(model_info['limit']['context'], 524288)
            self.assertEqual(model_info['limit']['output'], 8192)
            self.assertIn('variants', model_info)
            self.assertEqual(model_info['variants']['high']['reasoningEffort'], 'xhigh')
            self.assertEqual(model_info['variants']['medium']['reasoningEffort'], 'medium')

    def test_get_gguf_architecture_detection(self):
        """Verifies GGUF architecture header parsing."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_gguf = Path(tmp) / 'model.gguf'
            import struct
            # Minimal GGUF v3 header with general.architecture = 'qwen2'
            arch_key = b'general.architecture'
            arch_val = b'qwen2'
            header = (
                b'GGUF' +
                struct.pack('<I', 3) +       # version
                struct.pack('<Q', 0) +       # tensor_count
                struct.pack('<Q', 1) +       # kv_count = 1
                struct.pack('<Q', len(arch_key)) + arch_key +
                struct.pack('<I', 8) +       # string type
                struct.pack('<Q', len(arch_val)) + arch_val
            )
            fake_gguf.write_bytes(header)
            self.assertEqual(get_gguf_architecture(fake_gguf), 'qwen2')
            self.assertEqual(get_gguf_architecture(Path(tmp) / 'nonexistent.gguf'), '')


if __name__ == '__main__':
    unittest.main()
