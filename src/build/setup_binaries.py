# -*- coding: utf-8 -*-
"""
Interactive cross-platform installer, hardware & package scanner (`config/hardware.json`),
prerequisite verifier, source compiler for backend engines (llama.cpp, koboldcpp, ollama),
venv-scoped CLI coding agent installer (aider, opencode), VS Code Continue configurator,
and full uninstaller (`--uninstall`) across Windows, Linux, and macOS.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.paths import (
    BIN_DIR,
    CONFIG_DIR,
    HARDWARE_FILE,
    LOG_DIR,
    MODEL_DIR,
    SRC_DIR,
    get_executable_name,
    get_koboldcpp_binary,
    get_llama_server_binary,
    get_ollama_binary,
    get_platform_name,
    load_hardware,
    save_hardware,
    sync_config,
)
from src.common.process import stop_all_services


def _prompt_choice(prompt_text: str, valid_choices: List[str], default: str, non_interactive: bool = False) -> str:
    """Prompts user for a choice when running interactively, or returns default when non-interactive."""
    if non_interactive or not sys.stdin.isatty():
        return default
    while True:
        try:
            ans = input(f"{prompt_text} [{default}]: ").strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        if ans in valid_choices:
            return ans
        print(f"Please enter one of: {', '.join(valid_choices)}")


def find_windows_msvc() -> Optional[str]:
    """Locates vcvars64.bat on Windows via vswhere.exe if cl.exe is not already in PATH."""
    if os.name != 'nt':
        return None
    if shutil.which('cl.exe') or shutil.which('cl'):
        return 'PATH'
    vswhere = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    if vswhere.is_file():
        try:
            res = subprocess.run(
                [str(vswhere), '-latest', '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'],
                capture_output=True, text=True, timeout=5
            )
            install_dir = res.stdout.strip()
            if install_dir:
                vcvars = Path(install_dir) / 'VC' / 'Auxiliary' / 'Build' / 'vcvars64.bat'
                if vcvars.is_file():
                    return str(vcvars)
        except Exception:
            pass
    return None


def check_c_compiler() -> Tuple[bool, str]:
    """Checks whether a C/C++ compiler is installed across Windows, Linux, and macOS."""
    plat = get_platform_name()
    if plat == 'windows':
        msvc = find_windows_msvc()
        if msvc:
            return True, f"MSVC ({msvc})"
        if shutil.which('g++') or shutil.which('clang++'):
            return True, shutil.which('g++') or shutil.which('clang++') or ''
        return False, "Missing MSVC Build Tools (cl.exe) or MinGW g++"
    else:
        for comp in ('g++', 'clang++', 'c++'):
            found = shutil.which(comp)
            if found:
                return True, found
        return False, "Missing C++ compiler (g++ / clang++)"


def detect_gpu_acceleration() -> str:
    """
    Auto-detects available hardware acceleration:
    - 'cuda': NVIDIA GPU on Windows or Linux
    - 'metal': Apple Silicon / macOS
    - 'cpu': Fallback
    """
    plat = get_platform_name()
    if plat == 'macos':
        return 'metal'
    if shutil.which('nvidia-smi') or shutil.which('nvcc'):
        try:
            res = subprocess.run(['nvidia-smi', '-L'], capture_output=True, text=True, timeout=4)
            if res.returncode == 0 and 'GPU' in res.stdout:
                return 'cuda'
        except Exception:
            if shutil.which('nvcc'):
                return 'cuda'
    return 'cpu'


def _detect_ram_mb() -> int:
    """Detects total system RAM in MB across Windows, macOS, and Linux."""
    try:
        if os.name == 'nt':
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong),
                    ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('sullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys // (1024 * 1024))
        elif sys.platform == 'darwin':
            total_b = int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True, timeout=2).strip())
            return total_b // (1024 * 1024)
        else:
            for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines():
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 8192


def _detect_gpu_details() -> Dict[str, Any]:
    """Scans GPU name, VRAM, CUDA compute capability, and acceleration type."""
    plat = get_platform_name()
    accel = detect_gpu_acceleration()
    info: Dict[str, Any] = {
        'available': False,
        'acceleration': accel,
        'name': 'CPU Only',
        'vram_mb': 0,
        'count': 0,
        'compute_capability': '',
        'cuda_arch': '',
        'driver_version': '',
    }

    if plat == 'macos':
        ram_mb = _detect_ram_mb()
        name = 'Apple Metal GPU'
        try:
            res = subprocess.run(['system_profiler', 'SPDisplaysDataType'], capture_output=True, text=True, timeout=4)
            if res.returncode == 0:
                m = re.search(r'Chipset Model:\s*(.+)', res.stdout)
                if m:
                    name = m.group(1).strip()
        except Exception:
            pass
        info.update({
            'available': True,
            'acceleration': 'metal',
            'name': name,
            'vram_mb': int(ram_mb * 0.75),
            'count': 1,
        })
        return info

    if shutil.which('nvidia-smi'):
        try:
            res = subprocess.run(
                ['nvidia-smi', '--query-gpu=name,memory.total,compute_cap,driver_version', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=4
            )
            if res.returncode == 0 and res.stdout.strip():
                lines = [ln.strip() for ln in res.stdout.strip().splitlines() if ln.strip()]
                parts = [x.strip() for x in lines[0].split(',')]
                name = parts[0] if len(parts) > 0 else 'NVIDIA GPU'
                vram = int(float(parts[1])) if len(parts) > 1 and parts[1].replace('.', '').isdigit() else 0
                cc = parts[2] if len(parts) > 2 else ''
                drv = parts[3] if len(parts) > 3 else ''
                cuda_arch = cc.replace('.', '') if cc and re.match(r'^\d+\.\d+$', cc) else ''
                info.update({
                    'available': True,
                    'acceleration': 'cuda',
                    'name': name,
                    'vram_mb': vram,
                    'count': len(lines),
                    'compute_capability': cc,
                    'cuda_arch': cuda_arch,
                    'driver_version': drv,
                })
        except Exception:
            pass
    return info


def scan_and_save_hardware() -> Dict[str, Any]:
    """
    Scans system CPU, RAM, GPU, and installed build packages, computes optimal
    compilation flags and runtime defaults for llama.cpp, koboldcpp, and ollama,
    and writes the profile to `config/hardware.json`.
    """
    plat = get_platform_name()
    cpu_cores = os.cpu_count() or 4
    ram_mb = _detect_ram_mb()
    gpu = _detect_gpu_details()
    has_cc, cc_detail = check_c_compiler()
    nvcc_path = shutil.which('nvcc') or ('/usr/local/cuda/bin/nvcc' if Path('/usr/local/cuda/bin/nvcc').is_file() else None)

    packages = {
        'git': {'installed': bool(shutil.which('git')), 'path': shutil.which('git')},
        'c_compiler': {'installed': has_cc, 'detail': cc_detail},
        'cmake': {'installed': bool(shutil.which('cmake')), 'path': shutil.which('cmake')},
        'ninja': {'installed': bool(shutil.which('ninja')), 'path': shutil.which('ninja')},
        'make': {'installed': bool(shutil.which('make')), 'path': shutil.which('make')},
        'go': {'installed': bool(shutil.which('go')), 'path': shutil.which('go')},
        'nvcc': {'installed': bool(nvcc_path), 'path': nvcc_path},
        'npm': {'installed': bool(shutil.which('npm')), 'path': shutil.which('npm')},
        'curl': {'installed': bool(shutil.which('curl')), 'path': shutil.which('curl')},
    }

    # Auto-tune runtime settings based on detected VRAM / RAM & CPU cores
    vram_mb = gpu.get('vram_mb', 0)
    opt_threads = max(2, min(32, cpu_cores // 2 if cpu_cores >= 8 else max(2, cpu_cores - 1)))

    if gpu['available']:
        if vram_mb >= 20000:      # 24GB+ (e.g. RTX 3090/4090/5090 or 32GB+ Mac)
            opt_layers = 99
            opt_context = 32768
            opt_batch = 4096
            opt_ubatch = 512
        elif vram_mb >= 11000:    # 12GB - 16GB
            opt_layers = 48
            opt_context = 16384
            opt_batch = 2048
            opt_ubatch = 512
        elif vram_mb >= 7000:     # 8GB
            opt_layers = 32
            opt_context = 16384
            opt_batch = 2048
            opt_ubatch = 256
        else:                     # <= 6GB (e.g. 3GB - 4GB GTX 1050)
            opt_layers = 8
            opt_context = 8192
            opt_batch = 2048
            opt_ubatch = 256
        opt_flash_attn = 'on'
    else:
        opt_layers = 0
        opt_context = 8192 if ram_mb >= 16000 else 4096
        opt_batch = 1024
        opt_ubatch = 256
        opt_flash_attn = 'off'

    # Auto-tune build flags for llama.cpp, koboldcpp, and ollama
    parallel_jobs = max(2, min(16, cpu_cores - 1 if cpu_cores > 2 else 2))
    llama_cmake_flags = ['-DCMAKE_BUILD_TYPE=Release']
    if gpu['acceleration'] == 'cuda' and nvcc_path:
        llama_cmake_flags.extend([
            '-DGGML_CUDA=ON',
            '-DGGML_CUDA_FORCE_MMQ=ON',
            '-DGGML_CUDA_FA_ALL_QUANTS=ON',
        ])
        if gpu.get('cuda_arch'):
            llama_cmake_flags.append(f"-DCMAKE_CUDA_ARCHITECTURES={gpu['cuda_arch']}")
    elif gpu['acceleration'] == 'metal':
        llama_cmake_flags.append('-DGGML_METAL=ON')
    else:
        llama_cmake_flags.append('-DGGML_NATIVE=ON')

    kobold_make_flags = ['LLAMA_PORTABLE=1']
    if gpu['acceleration'] == 'cuda' and nvcc_path:
        kobold_make_flags.append('LLAMA_CUBLAS=1')

    hw_data = {
        'platform': plat,
        'arch': platform.machine(),
        'cpu': {
            'logical_cores': cpu_cores,
            'recommended_threads': opt_threads,
        },
        'ram': {
            'total_mb': ram_mb,
            'total_gb': round(ram_mb / 1024, 1),
        },
        'gpu': gpu,
        'packages': packages,
        'build_config': {
            'parallel_jobs': parallel_jobs,
            'llama_cpp_cmake_flags': llama_cmake_flags,
            'koboldcpp_make_flags': kobold_make_flags,
        },
        'runtime_defaults': {
            'layers': opt_layers,
            'context': opt_context,
            'threads': opt_threads,
            'batch': opt_batch,
            'ubatch': opt_ubatch,
            'flash_attn': opt_flash_attn,
            'kv_cache': 'q8_0',
        },
    }

    save_hardware(hw_data)
    return hw_data


def print_hardware_and_package_report(hw: Dict[str, Any]):
    """Displays the upfront hardware and package scan report to the user."""
    gpu = hw['gpu']
    print("\n=== Hardware & Package Scan (Saved to config/hardware.json) ===")
    print(f"  OS / Arch  : {hw['platform'].upper()} ({hw['arch']})")
    print(f"  CPU Cores  : {hw['cpu']['logical_cores']} logical (auto-tuned threads: {hw['runtime_defaults']['threads']})")
    print(f"  System RAM : {hw['ram']['total_gb']} GB ({hw['ram']['total_mb']} MB)")
    if gpu['available']:
        cc_str = f" | Compute {gpu['compute_capability']}" if gpu.get('compute_capability') else ""
        print(f"  GPU        : {gpu['name']} ({gpu['vram_mb']} MB VRAM | {gpu['acceleration'].upper()}{cc_str})")
    else:
        print("  GPU        : None detected (CPU mode)")
    print(f"  Auto-Tuned : layers={hw['runtime_defaults']['layers']}, ctx={hw['runtime_defaults']['context']}, batch={hw['runtime_defaults']['batch']}")

    print("\n--- Detected System Packages ---")
    for pkg_name, pkg_info in hw['packages'].items():
        status = "INSTALLED" if pkg_info['installed'] else "MISSING  "
        detail = pkg_info.get('path') or pkg_info.get('detail') or 'Not found'
        print(f"  [{status}] {pkg_name:14s} : {detail}")


def _pkg_install_cmd(plat: str, tool: str) -> List[str]:
    """Returns automated package manager install command for a given platform and tool."""
    if plat == 'windows':
        mapping = {
            'git': ['winget', 'install', '--id', 'Git.Git', '-e', '--source', 'winget'],
            'cmake': ['winget', 'install', '--id', 'Kitware.CMake', '-e', '--source', 'winget'],
            'ninja': ['winget', 'install', '--id', 'Ninja-build.Ninja', '-e', '--source', 'winget'],
            'compiler': [
                'winget', 'install', '--id', 'Microsoft.VisualStudio.2022.BuildTools',
                '--override', '--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive'
            ],
            'go': ['winget', 'install', '--id', 'GoLang.Go', '-e', '--source', 'winget'],
            'cuda': ['winget', 'install', '--id', 'Nvidia.CUDA', '-e', '--source', 'winget'],
        }
        return mapping.get(tool, [])

    elif plat == 'macos':
        if tool in ('compiler', 'make'):
            return ['xcode-select', '--install']
        mapping = {
            'git': ['brew', 'install', 'git'],
            'cmake': ['brew', 'install', 'cmake'],
            'ninja': ['brew', 'install', 'ninja'],
            'go': ['brew', 'install', 'go'],
        }
        return mapping.get(tool, [])

    else:  # linux
        if shutil.which('apt-get'):
            pkg_map = {
                'git': 'git',
                'compiler': 'build-essential',
                'cmake': 'cmake',
                'ninja': 'ninja-build',
                'make': 'make',
                'go': 'golang-go',
                'cuda': 'nvidia-cuda-toolkit',
            }
            pkg = pkg_map.get(tool)
            return ['sudo', 'apt-get', 'install', '-y', pkg] if pkg else []
        elif shutil.which('dnf'):
            pkg_map = {
                'git': 'git',
                'compiler': 'gcc-c++',
                'cmake': 'cmake',
                'ninja': 'ninja-build',
                'make': 'make',
                'go': 'golang',
                'cuda': 'cuda-toolkit',
            }
            pkg = pkg_map.get(tool)
            return ['sudo', 'dnf', 'install', '-y', pkg] if pkg else []
        elif shutil.which('pacman'):
            pkg_map = {
                'git': 'git',
                'compiler': 'base-devel',
                'cmake': 'cmake',
                'ninja': 'ninja',
                'make': 'make',
                'go': 'go',
                'cuda': 'cuda',
            }
            pkg = pkg_map.get(tool)
            return ['sudo', 'pacman', '-S', '--noconfirm', pkg] if pkg else []
        return []


def _manual_guide(plat: str, tool: str) -> str:
    """Returns human-readable manual installation instructions when user declines auto-install."""
    cmd = _pkg_install_cmd(plat, tool)
    if cmd:
        return f"Run manually: {' '.join(cmd)}"
    if plat == 'windows':
        return f"Install {tool} via https://winget.run or official installer and add to PATH."
    if plat == 'macos':
        return f"Install Homebrew (https://brew.sh) then run: brew install {tool}"
    return f"Install {tool} using your Linux distribution's package manager."


def inspect_requirements(needs_llama_src: bool, needs_kobold_src: bool, needs_ollama_src: bool, accel: str) -> List[Dict]:
    """
    Inspects system for required build tools based on selected engines to compile.
    Returns a list of requirement dicts: {name, found, detail, install_cmd, manual_guide}.
    """
    plat = get_platform_name()
    reqs = []

    if not (needs_llama_src or needs_kobold_src or needs_ollama_src):
        return reqs

    git_bin = shutil.which('git')
    reqs.append({
        'name': 'git',
        'found': bool(git_bin),
        'detail': git_bin or 'Not found in PATH',
        'install_cmd': _pkg_install_cmd(plat, 'git'),
        'manual_guide': _manual_guide(plat, 'git'),
    })

    has_cc, cc_detail = check_c_compiler()
    reqs.append({
        'name': 'c++ compiler',
        'found': has_cc,
        'detail': cc_detail,
        'install_cmd': _pkg_install_cmd(plat, 'compiler'),
        'manual_guide': _manual_guide(plat, 'compiler'),
    })

    if needs_llama_src or needs_ollama_src:
        cmake_bin = shutil.which('cmake')
        reqs.append({
            'name': 'cmake',
            'found': bool(cmake_bin),
            'detail': cmake_bin or 'Not found in PATH',
            'install_cmd': _pkg_install_cmd(plat, 'cmake'),
            'manual_guide': _manual_guide(plat, 'cmake'),
        })
        ninja_bin = shutil.which('ninja') or shutil.which('make')
        reqs.append({
            'name': 'ninja / make',
            'found': bool(ninja_bin),
            'detail': ninja_bin or 'Not found in PATH',
            'install_cmd': _pkg_install_cmd(plat, 'ninja'),
            'manual_guide': _manual_guide(plat, 'ninja'),
        })

    if needs_kobold_src and plat != 'windows':
        make_bin = shutil.which('make')
        reqs.append({
            'name': 'make',
            'found': bool(make_bin),
            'detail': make_bin or 'Not found in PATH',
            'install_cmd': _pkg_install_cmd(plat, 'make'),
            'manual_guide': _manual_guide(plat, 'make'),
        })

    if needs_ollama_src:
        go_bin = shutil.which('go')
        reqs.append({
            'name': 'golang (go)',
            'found': bool(go_bin),
            'detail': go_bin or 'Not found in PATH',
            'install_cmd': _pkg_install_cmd(plat, 'go'),
            'manual_guide': _manual_guide(plat, 'go'),
        })

    if accel == 'cuda':
        nvcc_bin = shutil.which('nvcc') or (Path('/usr/local/cuda/bin/nvcc').is_file() and '/usr/local/cuda/bin/nvcc')
        reqs.append({
            'name': 'cuda-toolkit (nvcc)',
            'found': bool(nvcc_bin),
            'detail': str(nvcc_bin) if nvcc_bin else 'NVIDIA GPU found, but nvcc compiler missing (will fallback to CPU if not installed)',
            'install_cmd': _pkg_install_cmd(plat, 'cuda'),
            'manual_guide': _manual_guide(plat, 'cuda'),
            'optional': True,
        })

    return reqs


def verify_and_install_requirements(reqs: List[Dict], non_interactive: bool = False) -> bool:
    """
    Displays requirement check results. If any required tool is missing, asks user
    permission to auto-install. If declined, prints manual installation guidance.
    """
    if not reqs:
        return True

    print("\n--- Build Requirements Verification ---")
    missing = []
    for r in reqs:
        status = "FOUND" if r['found'] else ("OPTIONAL-MISSING" if r.get('optional') else "MISSING")
        print(f"  [{status}] {r['name']:20s} : {r['detail']}")
        if not r['found']:
            missing.append(r)

    if not missing:
        print("All build requirements satisfied.\n")
        return True

    print("\nMissing requirements detected:")
    for m in missing:
        print(f"  - {m['name']}: {m['manual_guide']}")

    ans = _prompt_choice(
        "\nPermit auto-install of missing requirements now? (y/n)",
        ['y', 'yes', 'n', 'no'],
        default='n',
        non_interactive=non_interactive,
    )

    if ans in ('y', 'yes'):
        for m in missing:
            cmd = m.get('install_cmd')
            if cmd:
                print(f"Installing {m['name']} -> {' '.join(cmd)}")
                try:
                    subprocess.run(cmd, check=True)
                except Exception as e:
                    print(f"Warning: Auto-install failed for {m['name']}: {e}")
                    print(f"Guidance: {m['manual_guide']}")
        # Re-scan hardware.json after package installation
        scan_and_save_hardware()
        mandatory_ok = True
        for m in missing:
            if not m.get('optional'):
                if m['name'] == 'c++ compiler':
                    ok, _ = check_c_compiler()
                elif m['name'] == 'ninja / make':
                    ok = bool(shutil.which('ninja') or shutil.which('make'))
                elif m['name'] == 'golang (go)':
                    ok = bool(shutil.which('go'))
                else:
                    ok = bool(shutil.which(m['name'].split()[0]))
                if not ok:
                    mandatory_ok = False
        return mandatory_ok
    else:
        print("\n=== Manual Installation Guidance ===")
        for m in missing:
            print(f"  * {m['name']}: {m['manual_guide']}")
        print("After installing the tools above, re-run setup or choose prebuilt binaries.\n")
        mandatory_missing = [m for m in missing if not m.get('optional')]
        return len(mandatory_missing) == 0


def download_file(url: str, dest: Path) -> Path:
    """Downloads a URL to dest with user-agent header."""
    print(f"Downloading {url} ...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={'User-Agent': 'llmCode-Installer/1.0'})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, 'wb') as f:
        shutil.copyfileobj(resp, f)
    print(f"Saved to {dest}")
    return dest


# ==============================================================================
# Engine 1: llama.cpp (Hardware-Tuned via config/hardware.json)
# ==============================================================================
def compile_llama_cpp(accel: str) -> bool:
    """Clones and compiles llama.cpp inside src/llama.cpp using flags from config/hardware.json."""
    hw = load_hardware() or scan_and_save_hardware()
    src_dir = SRC_DIR / 'llama.cpp'
    if not (src_dir / 'CMakeLists.txt').is_file():
        print(f"Cloning llama.cpp into {src_dir} ...")
        subprocess.run(['git', 'clone', '--depth', '1', 'https://github.com/ggml-org/llama.cpp.git', str(src_dir)], check=True)

    cmake_flags = hw.get('build_config', {}).get('llama_cpp_cmake_flags', ['-DCMAKE_BUILD_TYPE=Release'])
    use_cuda = any('GGML_CUDA=ON' in f for f in cmake_flags)
    use_metal = any('GGML_METAL=ON' in f for f in cmake_flags)

    build_subdir = 'build-cuda' if use_cuda else ('build-metal' if use_metal else 'build')
    build_dir = src_dir / build_subdir

    cmake_args = ['cmake', '-S', str(src_dir), '-B', str(build_dir)]
    if shutil.which('ninja'):
        cmake_args.extend(['-G', 'Ninja'])
    cmake_args.extend(cmake_flags)

    jobs = str(hw.get('build_config', {}).get('parallel_jobs', 4))
    build_cmd = ['cmake', '--build', str(build_dir), '--config', 'Release', '--target', 'llama-server', '-j', jobs]

    print(f"Compiling llama.cpp with hardware flags: {' '.join(cmake_flags)} (-j {jobs})")
    try:
        if os.name == 'nt':
            msvc = find_windows_msvc()
            if msvc and msvc != 'PATH':
                cfg_line = f'call "{msvc}" && ' + ' '.join(f'"{a}"' if ' ' in a else a for a in cmake_args)
                bld_line = f'call "{msvc}" && ' + ' '.join(f'"{a}"' if ' ' in a else a for a in build_cmd)
                subprocess.run(cfg_line, shell=True, check=True)
                subprocess.run(bld_line, shell=True, check=True)
            else:
                subprocess.run(cmake_args, check=True)
                subprocess.run(build_cmd, check=True)
        else:
            subprocess.run(cmake_args, check=True)
            subprocess.run(build_cmd, check=True)
        print(f"llama.cpp compiled successfully in {build_dir}")
        return True
    except Exception as e:
        print(f"Error compiling llama.cpp: {e}")
        return False


def download_prebuilt_llama_cpp(accel: str) -> bool:
    """Downloads official prebuilt llama.cpp release archive from GitHub for Windows, macOS, or Linux."""
    plat = get_platform_name()
    dest_dir = BIN_DIR / plat
    dest_dir.mkdir(parents=True, exist_ok=True)

    api_url = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    req = urllib.request.Request(api_url, headers={'User-Agent': 'llmCode-Installer/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            release_data = json.loads(r.read())
    except Exception as e:
        print(f"Failed to query llama.cpp GitHub release: {e}")
        return False

    assets = release_data.get('assets', [])
    target_asset = None

    for asset in assets:
        name = asset.get('name', '').lower()
        if plat == 'windows' and 'bin-win' in name and 'x64' in name:
            if accel == 'cuda' and ('cuda' in name or 'cu12' in name):
                target_asset = asset
                break
            elif accel != 'cuda' and 'avx2' in name:
                target_asset = asset
                break
        elif plat == 'macos' and 'bin-macos' in name:
            arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64'
            if arch in name:
                target_asset = asset
                break
        elif plat == 'linux' and ('bin-ubuntu' in name or 'bin-linux' in name) and 'x64' in name:
            target_asset = asset
            break

    if not target_asset:
        print(f"No matching prebuilt llama.cpp archive found for {plat}.")
        return False

    archive_path = dest_dir / target_asset['name']
    download_file(target_asset['browser_download_url'], archive_path)

    if archive_path.name.endswith('.zip'):
        with zipfile.ZipFile(archive_path, 'r') as zf:
            zf.extractall(dest_dir)
    elif archive_path.name.endswith(('.tar.gz', '.tgz')):
        with tarfile.open(archive_path, 'r:gz') as tf:
            tf.extractall(dest_dir)
    archive_path.unlink(missing_ok=True)

    exe_name = get_executable_name('llama-server')
    for p in dest_dir.rglob(exe_name):
        if p.is_file():
            if os.name != 'nt':
                p.chmod(0o755)
            break

    print(f"Prebuilt llama.cpp installed in {dest_dir}")
    return True


# ==============================================================================
# Engine 2: KoboldCpp (Hardware-Tuned via config/hardware.json)
# ==============================================================================
def compile_koboldcpp(accel: str) -> bool:
    """Clones and compiles KoboldCpp inside src/koboldcpp using flags from config/hardware.json."""
    hw = load_hardware() or scan_and_save_hardware()
    src_dir = SRC_DIR / 'koboldcpp'
    if not (src_dir / 'koboldcpp.py').is_file():
        print(f"Cloning KoboldCpp into {src_dir} ...")
        subprocess.run(['git', 'clone', '--depth', '1', 'https://github.com/LostRuins/koboldcpp.git', str(src_dir)], check=True)

    plat = get_platform_name()
    if plat == 'windows':
        cmake_args = ['cmake', '-S', str(src_dir), '-B', str(src_dir / 'build'), '-DCMAKE_BUILD_TYPE=Release']
        try:
            subprocess.run(cmake_args, check=True)
            subprocess.run(['cmake', '--build', str(src_dir / 'build'), '--config', 'Release'], check=True)
            return True
        except Exception as e:
            print(f"Windows KoboldCpp local build note ({e}); downloading official prebuilt binary fallback...")
            return download_prebuilt_koboldcpp(accel)
    else:
        jobs = str(hw.get('build_config', {}).get('parallel_jobs', 4))
        make_flags = hw.get('build_config', {}).get('koboldcpp_make_flags', ['LLAMA_PORTABLE=1'])
        make_cmd = ['make', '-C', str(src_dir), f'-j{jobs}'] + make_flags
        print(f"Compiling KoboldCpp with hardware flags: {' '.join(make_flags)} (-j {jobs})")
        try:
            subprocess.run(make_cmd, check=True)
            print(f"KoboldCpp compiled successfully in {src_dir}")
            return True
        except Exception as e:
            print(f"Error compiling KoboldCpp: {e}")
            return False


def download_prebuilt_koboldcpp(accel: str) -> bool:
    """Downloads official standalone KoboldCpp binary from GitHub releases into bin/."""
    plat = get_platform_name()
    exe_name = get_executable_name('koboldcpp')
    dest = BIN_DIR / exe_name

    if plat == 'windows':
        asset_name = 'koboldcpp.exe' if accel == 'cuda' else 'koboldcpp_nocuda.exe'
    elif plat == 'macos':
        asset_name = 'koboldcpp-mac-arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'koboldcpp-mac-x64'
    else:
        asset_name = 'koboldcpp-linux-x64-cuda1210' if accel == 'cuda' else 'koboldcpp-linux-x64-nocuda'

    url = f"https://github.com/LostRuins/koboldcpp/releases/latest/download/{asset_name}"
    try:
        download_file(url, dest)
        if os.name != 'nt':
            dest.chmod(0o755)
        return True
    except Exception as e:
        print(f"Failed to download prebuilt KoboldCpp ({url}): {e}")
        return False


# ==============================================================================
# Engine 3: Ollama (Hardware-Tuned via config/hardware.json)
# ==============================================================================
def compile_ollama(accel: str) -> bool:
    """Clones and compiles Ollama from source inside src/ollama using config/hardware.json."""
    hw = load_hardware() or scan_and_save_hardware()
    src_dir = SRC_DIR / 'ollama'
    if not (src_dir / 'go.mod').is_file():
        print(f"Cloning Ollama into {src_dir} ...")
        subprocess.run(['git', 'clone', '--depth', '1', 'https://github.com/ollama/ollama.git', str(src_dir)], check=True)

    exe_name = get_executable_name('ollama')
    jobs = str(hw.get('build_config', {}).get('parallel_jobs', 4))
    try:
        if (src_dir / 'CMakeLists.txt').is_file() and shutil.which('cmake'):
            cmake_cmd = ['cmake', '-S', str(src_dir), '-B', str(src_dir / 'build'), '-DCMAKE_BUILD_TYPE=Release']
            if hw['gpu'].get('cuda_arch'):
                cmake_cmd.append(f"-DCMAKE_CUDA_ARCHITECTURES={hw['gpu']['cuda_arch']}")
            subprocess.run(cmake_cmd, check=True)
            subprocess.run(['cmake', '--build', str(src_dir / 'build'), '-j', jobs], check=True)
        subprocess.run(['go', 'build', '-o', str(src_dir / exe_name), '.'], cwd=str(src_dir), check=True)
        print(f"Ollama compiled successfully at {src_dir / exe_name}")
        return True
    except Exception as e:
        print(f"Error compiling Ollama from source: {e}")
        return False


def download_prebuilt_ollama() -> bool:
    """Downloads official prebuilt Ollama binary distribution into bin/ollama_dist or bin/."""
    plat = get_platform_name()
    dist_dir = BIN_DIR / 'ollama_dist'
    dist_dir.mkdir(parents=True, exist_ok=True)

    if plat == 'windows':
        url = "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip"
        archive = dist_dir / "ollama-windows-amd64.zip"
        try:
            download_file(url, archive)
            with zipfile.ZipFile(archive, 'r') as zf:
                zf.extractall(BIN_DIR / 'windows')
            archive.unlink(missing_ok=True)
            return True
        except Exception as e:
            print(f"Failed to download Windows Ollama: {e}")
            return False

    elif plat == 'macos':
        url = "https://github.com/ollama/ollama/releases/latest/download/ollama-darwin.tgz"
        archive = dist_dir / "ollama-darwin.tgz"
        try:
            download_file(url, archive)
            with tarfile.open(archive, 'r:gz') as tf:
                tf.extractall(BIN_DIR)
            archive.unlink(missing_ok=True)
            ollama_bin = BIN_DIR / 'ollama'
            if ollama_bin.is_file():
                ollama_bin.chmod(0o755)
            return True
        except Exception as e:
            print(f"Failed to download macOS Ollama: {e}")
            return False

    else:  # linux
        arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'amd64'
        url = f"https://github.com/ollama/ollama/releases/latest/download/ollama-linux-{arch}.tgz"
        archive = dist_dir / f"ollama-linux-{arch}.tgz"
        try:
            download_file(url, archive)
            with tarfile.open(archive, 'r:gz') as tf:
                tf.extractall(dist_dir)
            archive.unlink(missing_ok=True)
            bin_path = dist_dir / 'bin' / 'ollama'
            if bin_path.is_file():
                bin_path.chmod(0o755)
                link_path = BIN_DIR / 'ollama'
                if link_path.exists() or link_path.is_symlink():
                    link_path.unlink(missing_ok=True)
                link_path.symlink_to(bin_path)
            return True
        except Exception as e:
            print(f"Failed to download Linux Ollama: {e}")
            return False


# ==============================================================================
# CLI Agents: Aider & OpenCode (Installed directly inside llm-dash .venv)
# ==============================================================================
def _install_opencode_into_venv(venv_dir: Path) -> bool:
    """
    Installs OpenCode CLI directly inside the llm-dash virtualenv
    (`.venv/bin/opencode` on Linux/macOS or `.venv/Scripts/opencode.exe` on Windows).
    Tries GitHub release binary first, then local npm --prefix .venv fallback.
    """
    plat = get_platform_name()
    venv_bin_dir = venv_dir / ('Scripts' if os.name == 'nt' else 'bin')
    venv_bin_dir.mkdir(parents=True, exist_ok=True)
    exe_name = get_executable_name('opencode')
    target_bin = venv_bin_dir / exe_name

    if target_bin.is_file():
        print(f"OpenCode CLI already present in venv at {target_bin}")
        return True

    # 1. Try downloading official standalone binary release from GitHub into .venv/bin or .venv/Scripts
    api_url = "https://api.github.com/repos/sst/opencode/releases/latest"
    req = urllib.request.Request(api_url, headers={'User-Agent': 'llmCode-Installer/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            rel = json.loads(r.read())
        arch_is_arm = platform.machine().lower() in ('arm64', 'aarch64')
        arch_tok = 'arm64' if arch_is_arm else 'x64'
        plat_tok = 'windows' if plat == 'windows' else ('darwin' if plat == 'macos' else 'linux')

        matched_asset = None
        for asset in rel.get('assets', []):
            aname = asset.get('name', '').lower()
            if plat_tok in aname and arch_tok in aname and aname.endswith(('.zip', '.tar.gz', '.tgz')):
                matched_asset = asset
                break

        if matched_asset:
            tmp_archive = venv_bin_dir / matched_asset['name']
            download_file(matched_asset['browser_download_url'], tmp_archive)
            extract_tmp = venv_bin_dir / '_opencode_tmp'
            extract_tmp.mkdir(parents=True, exist_ok=True)
            if tmp_archive.name.endswith('.zip'):
                with zipfile.ZipFile(tmp_archive, 'r') as zf:
                    zf.extractall(extract_tmp)
            else:
                with tarfile.open(tmp_archive, 'r:gz') as tf:
                    tf.extractall(extract_tmp)
            tmp_archive.unlink(missing_ok=True)

            for p in extract_tmp.rglob(exe_name):
                if p.is_file():
                    shutil.move(str(p), str(target_bin))
                    if os.name != 'nt':
                        target_bin.chmod(0o755)
                    break
            shutil.rmtree(extract_tmp, ignore_errors=True)
            if target_bin.is_file():
                print(f"OpenCode installed into venv at: {target_bin}")
                return True
    except Exception:
        pass

    # 2. Fallback: install via npm scoped to venv_dir (`npm install --prefix <venv_dir> opencode-ai`)
    if shutil.which('npm'):
        print(f"Installing OpenCode into {venv_dir} via npm...")
        try:
            subprocess.run(['npm', 'install', '--prefix', str(venv_dir), 'opencode-ai'], check=True)
            npm_bin = venv_dir / 'node_modules' / '.bin' / ('opencode.cmd' if os.name == 'nt' else 'opencode')
            if npm_bin.is_file():
                if os.name == 'nt':
                    wrapper = venv_bin_dir / 'opencode.cmd'
                    wrapper.write_text(f'@echo off\r\n"{npm_bin}" %*\r\n', encoding='utf-8')
                else:
                    if target_bin.exists() or target_bin.is_symlink():
                        target_bin.unlink(missing_ok=True)
                    target_bin.symlink_to(npm_bin)
                print(f"OpenCode installed in venv via npm ({npm_bin}).")
                return True
        except Exception as e:
            print(f"Warning: venv npm install failed ({e}).")

    print("Manual guidance for OpenCode: Install Node.js (https://nodejs.org) or download OpenCode into src/LLMServer/.venv/bin.")
    return False


def install_cli_agents(venv_python: str, non_interactive: bool = False):
    """Prompts user to install Aider and/or OpenCode CLI coding agents inside the llm-dash .venv."""
    venv_dir = REPO_ROOT / 'src' / 'LLMServer' / '.venv'
    print("\n=== CLI Coding Agents Setup (Installed in llm-dash .venv) ===")
    print("  [1] Install BOTH Aider & OpenCode in .venv (Recommended)")
    print("  [2] Install Aider only in .venv")
    print("  [3] Install OpenCode only in .venv")
    print("  [4] Skip CLI agent installation")

    choice = _prompt_choice("Select CLI agent option (1/2/3/4)", ['1', '2', '3', '4'], default='1', non_interactive=non_interactive)

    if choice in ('1', '2'):
        print("\nInstalling Aider CLI (aider-chat) into llm-dash .venv...")
        try:
            subprocess.run([venv_python, '-m', 'pip', 'install', '-q', 'aider-chat'], check=True)
            print("Aider installed successfully in llm-dash .venv.")
        except Exception as e:
            print(f"Warning: Aider pip install encountered an issue: {e}")

    if choice in ('1', '3'):
        print("\nInstalling OpenCode CLI into llm-dash .venv...")
        _install_opencode_into_venv(venv_dir)


# ==============================================================================
# VS Code Continue Configurator (Standard Aliases: llamacpp, koboldcpp, ollama)
# ==============================================================================
def setup_continue_config(continue_dir: Optional[Path] = None, port: int = 8080, non_interactive: bool = False) -> Optional[Path]:
    """
    Checks if VS Code Continue configuration (`~/.continue/config.yaml` or `~/.continue/config.json`)
    exists. If found, offers to configure standard backend aliases (`llamacpp`, `koboldcpp`, `ollama`)
    pointing to `http://127.0.0.1:<port>/v1`.
    """
    c_dir = continue_dir or (Path.home() / '.continue')
    yaml_path = c_dir / 'config.yaml'
    json_path = c_dir / 'config.json'

    target_file = yaml_path if yaml_path.is_file() else (json_path if json_path.is_file() else None)
    if not target_file:
        return None

    print(f"\nFound VS Code Continue configuration at: {target_file}")
    ans = _prompt_choice(
        "Configure standard llmCode backends (llamacpp, koboldcpp, ollama) in Continue? (y/n)",
        ['y', 'yes', 'n', 'no'],
        default='y',
        non_interactive=non_interactive,
    )
    if ans not in ('y', 'yes'):
        return None

    api_base = f"http://127.0.0.1:{port}/v1"

    if target_file.suffix.lower() in ('.yaml', '.yml'):
        content = target_file.read_text(encoding='utf-8')
        marker_start = "# >>> llmCode standard backends >>>"
        marker_end = "# <<< llmCode standard backends <<<"
        block = (
            f"{marker_start}\n"
            f"  - name: llamacpp\n"
            f"    provider: openai\n"
            f"    model: llamacpp\n"
            f"    apiBase: {api_base}\n"
            f"    apiKey: EMPTY\n"
            f"    roles:\n"
            f"      - chat\n"
            f"      - edit\n"
            f"      - apply\n"
            f"  - name: koboldcpp\n"
            f"    provider: openai\n"
            f"    model: koboldcpp\n"
            f"    apiBase: {api_base}\n"
            f"    apiKey: EMPTY\n"
            f"    roles:\n"
            f"      - chat\n"
            f"      - edit\n"
            f"      - apply\n"
            f"  - name: ollama\n"
            f"    provider: openai\n"
            f"    model: ollama\n"
            f"    apiBase: {api_base}\n"
            f"    apiKey: EMPTY\n"
            f"    roles:\n"
            f"      - chat\n"
            f"      - edit\n"
            f"      - apply\n"
            f"{marker_end}"
        )
        if marker_start in content and marker_end in content:
            pattern = re.compile(re.escape(marker_start) + r".*?" + re.escape(marker_end), re.DOTALL)
            new_content = pattern.sub(block, content)
        elif 'models:' in content:
            new_content = content.replace('models:', f"models:\n{block}", 1)
        else:
            new_content = content.rstrip() + f"\n\nmodels:\n{block}\n"
        target_file.write_text(new_content, encoding='utf-8')
        print(f"Updated {target_file} with standard aliases: llamacpp, koboldcpp, ollama")
        return target_file

    elif target_file.suffix.lower() == '.json':
        try:
            data = json.loads(target_file.read_text(encoding='utf-8'))
        except Exception:
            data = {}
        models = data.get('models', [])
        if not isinstance(models, list):
            models = []
        # Remove old llmCode standard entries before re-inserting
        models = [m for m in models if m.get('title') not in ('llamacpp', 'koboldcpp', 'ollama') and m.get('model') not in ('llamacpp', 'koboldcpp', 'ollama')]
        for alias in ('llamacpp', 'koboldcpp', 'ollama'):
            models.append({
                'title': alias,
                'provider': 'openai',
                'model': alias,
                'apiBase': api_base,
                'apiKey': 'EMPTY',
            })
        data['models'] = models
        target_file.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
        print(f"Updated {target_file} with standard aliases: llamacpp, koboldcpp, ollama")
        return target_file

    return None


# ==============================================================================
# Full Uninstaller (--uninstall)
# ==============================================================================
def uninstall_all(repo_root: Optional[Path] = None, non_interactive: bool = False) -> bool:
    """
    Completely uninstalls llmCode artifacts:
    - Stops all running llm-dash and backend engine processes
    - Deletes src/LLMServer/.venv
    - Deletes src/llama.cpp, src/koboldcpp, src/ollama
    - Deletes downloaded binaries in bin/ (preserving bin/llmCode.desktop and bin/llmCode.svg)
    - Deletes all downloaded GGUF models in models/
    - Deletes config/ and logs/
    """
    root = repo_root or REPO_ROOT
    print("\n=== llmCode Complete Uninstaller (--uninstall) ===")
    print("This will permanently remove:")
    print(f"  - Virtualenv & CLI agents : {root / 'src' / 'LLMServer' / '.venv'}")
    print(f"  - Engine sources & builds : {root / 'src' / 'llama.cpp'}, koboldcpp, ollama")
    print(f"  - Prebuilt binaries       : {root / 'bin' / '*'}")
    print(f"  - All downloaded models   : {root / 'models' / '*.gguf'}")
    print(f"  - Config & Logs           : {root / 'config'}, {root / 'logs'}")

    ans = _prompt_choice(
        "\nConfirm complete uninstall of all llmCode components and models? (y/n)",
        ['y', 'yes', 'n', 'no'],
        default='n',
        non_interactive=non_interactive,
    )
    if ans not in ('y', 'yes') and not non_interactive:
        print("Uninstall cancelled.")
        return False

    if root == REPO_ROOT:
        stop_all_services()

    # 1. Remove virtualenv and engine source directories
    dirs_to_remove = [
        root / 'src' / 'LLMServer' / '.venv',
        root / 'src' / 'llama.cpp',
        root / 'src' / 'koboldcpp',
        root / 'src' / 'ollama',
        root / 'bin' / 'windows',
        root / 'bin' / 'linux',
        root / 'bin' / 'macos',
        root / 'bin' / 'ollama_dist',
        root / 'config',
        root / 'logs',
        root / 'models',
    ]
    for d in dirs_to_remove:
        if d.exists():
            print(f"Removing {d} ...")
            shutil.rmtree(d, ignore_errors=True)

    # 2. Remove standalone binaries inside bin/ while keeping .desktop and .svg assets
    bin_dir = root / 'bin'
    if bin_dir.is_dir():
        for item in bin_dir.iterdir():
            if item.name not in ('llmCode.desktop', 'llmCode.svg'):
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)

    # Recreate empty models, config, logs folders so repo structure stays clean
    for empty_d in (root / 'models', root / 'config', root / 'logs'):
        empty_d.mkdir(parents=True, exist_ok=True)

    print("llmCode completely uninstalled.")
    return True


def main(non_interactive: bool = False, uninstall: bool = False):
    if uninstall:
        uninstall_all(non_interactive=non_interactive)
        return

    # 1. Scan hardware and available packages upfront and write config/hardware.json
    hw = scan_and_save_hardware()
    print_hardware_and_package_report(hw)

    accel = hw['gpu']['acceleration']

    # 2. Prompt per engine (compile from source in src/ vs prebuilt binary in bin/)
    engines = [
        ('llama.cpp', get_llama_server_binary()),
        ('koboldcpp', get_koboldcpp_binary()),
        ('ollama', get_ollama_binary()),
    ]

    selections = {}
    for eng_name, existing_bin in engines:
        exists_note = f" [Already present at {existing_bin}]" if existing_bin.is_file() else " [Not installed]"
        print(f"\nBackend Engine: {eng_name}{exists_note}")
        print(f"  [1] Compile from source in src/{eng_name} (auto-tuned via config/hardware.json)")
        print(f"  [2] Download prebuilt binary into bin/")
        print(f"  [3] Skip (keep current)")
        default_opt = '3' if existing_bin.is_file() else ('1' if eng_name != 'ollama' else '2')
        selections[eng_name] = _prompt_choice(
            f"Choose action for {eng_name} (1/2/3)",
            ['1', '2', '3'],
            default=default_opt,
            non_interactive=non_interactive,
        )

    needs_llama_src = selections['llama.cpp'] == '1'
    needs_kobold_src = selections['koboldcpp'] == '1'
    needs_ollama_src = selections['ollama'] == '1'

    # 3. Verify build requirements for selected source compilations & ask permission to auto-install
    reqs = inspect_requirements(needs_llama_src, needs_kobold_src, needs_ollama_src, accel)
    can_compile = verify_and_install_requirements(reqs, non_interactive=non_interactive)

    # 4. Execute engine compilation or download
    if selections['llama.cpp'] == '1':
        if can_compile:
            if not compile_llama_cpp(accel):
                print("Falling back to prebuilt llama.cpp binary...")
                download_prebuilt_llama_cpp(accel)
        else:
            print("Skipping llama.cpp source build due to missing compilers; downloading prebuilt binary...")
            download_prebuilt_llama_cpp(accel)
    elif selections['llama.cpp'] == '2':
        download_prebuilt_llama_cpp(accel)

    if selections['koboldcpp'] == '1':
        if can_compile:
            compile_koboldcpp(accel)
        else:
            print("Skipping koboldcpp source build; downloading prebuilt binary...")
            download_prebuilt_koboldcpp(accel)
    elif selections['koboldcpp'] == '2':
        download_prebuilt_koboldcpp(accel)

    if selections['ollama'] == '1':
        if can_compile and shutil.which('go'):
            if not compile_ollama(accel):
                print("Falling back to prebuilt Ollama binary...")
                download_prebuilt_ollama()
        else:
            print("Go compiler not present; downloading prebuilt Ollama binary...")
            download_prebuilt_ollama()
    elif selections['ollama'] == '2':
        download_prebuilt_ollama()

    # 5. Install CLI coding agents (Aider & OpenCode) inside llm-dash .venv
    venv_py = REPO_ROOT / 'src' / 'LLMServer' / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python3')
    py_exe = str(venv_py) if venv_py.is_file() else sys.executable
    install_cli_agents(py_exe, non_interactive=non_interactive)

    # 6. Offer to configure VS Code Continue if ~/.continue/config.yaml or config.json exists
    setup_continue_config(non_interactive=non_interactive)

    # 7. Sync all resolved paths & hardware-tuned defaults into config/config.json
    cfg = sync_config(apply_hardware_defaults=True)
    print("\n=== Configuration Database Updated (config/config.json & config/hardware.json) ===")
    print(f"  Hardware Profile : {HARDWARE_FILE}")
    print(f"  llama.cpp        : {cfg['binaries']['llama_cpp']} (ready={cfg['binaries']['llama_cpp_exists']})")
    print(f"  koboldcpp        : {cfg['binaries']['koboldcpp']} (ready={cfg['binaries']['koboldcpp_exists']})")
    print(f"  ollama           : {cfg['binaries']['ollama']} (ready={cfg['binaries']['ollama_exists']})")
    print(f"  aider (.venv)    : {cfg['agents']['aider'] or 'not installed'}")
    print(f"  opencode (.venv) : {cfg['agents']['opencode'] or 'not installed'}")


if __name__ == '__main__':
    non_int = '--non-interactive' in sys.argv or '-y' in sys.argv
    uninst = '--uninstall' in sys.argv
    main(non_interactive=non_int, uninstall=uninst)
