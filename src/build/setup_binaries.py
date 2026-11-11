# -*- coding: utf-8 -*-
"""
Interactive cross-platform installer, hardware & package scanner (`config/hardware.json`),
prerequisite verifier, source compiler for backend engines (llama.cpp, koboldcpp, ollama),
venv-scoped CLI coding agent installer (opencode, claude, codex),
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
    parse_version_tuple,
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


def find_windows_msvc(prefer_cuda_compat: bool = True) -> Optional[str]:
    """Locates vcvars64.bat on Windows via vswhere.exe.
    When prefer_cuda_compat is True, searches for Visual Studio 2022 (version [17.0,18.0))
    first, then Visual Studio 2019 (version [16.0,17.0)), to ensure compatibility with
    NVIDIA CUDA host compiler requirements before falling back to -latest.
    """
    if os.name != 'nt':
        return None
    vswhere = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    if vswhere.is_file():
        if prefer_cuda_compat:
            for ver_range in ('[17.0,18.0)', '[16.0,17.0)'):
                try:
                    res = subprocess.run(
                        [str(vswhere), '-version', ver_range, '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'],
                        capture_output=True, text=True, timeout=5
                    )
                    install_dir = res.stdout.strip()
                    if install_dir:
                        vcvars = Path(install_dir) / 'VC' / 'Auxiliary' / 'Build' / 'vcvars64.bat'
                        if vcvars.is_file():
                            return str(vcvars)
                except Exception:
                    pass
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
    if shutil.which('cl.exe') or shutil.which('cl'):
        return 'PATH'
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


def _find_nvidia_smi() -> Optional[str]:
    """Locates nvidia-smi across PATH and standard Windows/Linux driver directories."""
    found = shutil.which('nvidia-smi')
    if found:
        return found
    if os.name == 'nt':
        win_candidates = [
            Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'nvidia-smi.exe',
            Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'NVIDIA Corporation' / 'NVSMI' / 'nvidia-smi.exe',
        ]
        for c in win_candidates:
            if c.is_file():
                return str(c)
        driver_store = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'DriverStore' / 'FileRepository'
        if driver_store.is_dir():
            for p in sorted(driver_store.glob('nv*/nvidia-smi.exe'), reverse=True):
                if p.is_file():
                    return str(p)
    else:
        for p_str in ('/usr/bin/nvidia-smi', '/usr/local/nvidia/bin/nvidia-smi', '/opt/cuda/bin/nvidia-smi'):
            if Path(p_str).is_file():
                return p_str
    return None


def _find_nvcc() -> Optional[str]:
    """Locates CUDA compiler (nvcc) across PATH, CUDA_PATH/CUDA_HOME, and Linux/Windows toolkit directories."""
    found = shutil.which('nvcc')
    if found:
        return found
    exe = get_executable_name('nvcc')
    for env_key in ('CUDA_PATH', 'CUDA_HOME'):
        env_val = os.environ.get(env_key, '').strip()
        if env_val:
            cand = Path(env_val) / 'bin' / exe
            if cand.is_file():
                return str(cand)
    if os.name == 'nt':
        cuda_base = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'NVIDIA GPU Computing Toolkit' / 'CUDA'
        if cuda_base.is_dir():
            for p in sorted(cuda_base.glob(f'v*/bin/{exe}'), reverse=True):
                if p.is_file():
                    return str(p)
    else:
        for p_str in (
            '/usr/local/cuda/bin/nvcc',
            '/opt/cuda/bin/nvcc',
            '/usr/lib/nvidia-cuda-toolkit/bin/nvcc',
            '/usr/bin/nvcc',
        ):
            if Path(p_str).is_file():
                return p_str
        usr_local = Path('/usr/local')
        if usr_local.is_dir():
            for p in sorted(usr_local.glob('cuda-*/bin/nvcc'), reverse=True):
                if p.is_file():
                    return str(p)
    return None


def _detect_apple_silicon_p_cores() -> Optional[int]:
    """Queries macOS sysctl hw.perflevel0.physicalcpu for Apple Silicon Performance core count."""
    if get_platform_name() != 'macos':
        return None
    try:
        res = subprocess.run(
            ['sysctl', '-n', 'hw.perflevel0.physicalcpu'],
            capture_output=True, text=True, timeout=2
        )
        if res.returncode == 0 and res.stdout.strip().isdigit():
            val = int(res.stdout.strip())
            if val > 0:
                return val
    except Exception:
        pass
    return None


def _strip_macos_quarantine(target_path: Path) -> None:
    """Removes macOS Gatekeeper com.apple.quarantine attributes and sets 0o755 on binaries/dylibs."""
    if get_platform_name() != 'macos' or not target_path.exists():
        return
    try:
        subprocess.run(['xattr', '-cr', str(target_path)], capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        if target_path.is_file():
            target_path.chmod(0o755)
        elif target_path.is_dir():
            for p in target_path.rglob('*'):
                if p.is_file() and (p.suffix in ('.dylib', '.so') or 'llama-' in p.name or p.name in ('koboldcpp', 'ollama', 'opencode')):
                    try:
                        p.chmod(0o755)
                    except Exception:
                        pass
    except Exception:
        pass


def _detect_windows_gpu_wmi() -> Optional[Dict[str, Any]]:
    """Queries Windows Win32_VideoController via PowerShell CIM as a fallback when nvidia-smi is unavailable."""
    if os.name != 'nt':
        return None
    ps_bin = shutil.which('powershell') or shutil.which('pwsh')
    if not ps_bin:
        return None
    try:
        cmd = [
            ps_bin, '-NoProfile', '-NonInteractive', '-Command',
            'Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json -Compress'
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        if res.returncode == 0 and res.stdout.strip():
            raw = json.loads(res.stdout.strip())
            controllers = raw if isinstance(raw, list) else [raw]
            for ctrl in controllers:
                if not isinstance(ctrl, dict):
                    continue
                name = str(ctrl.get('Name') or '').strip()
                if 'nvidia' in name.lower():
                    adapter_ram = int(ctrl.get('AdapterRAM') or 0)
                    vram_mb = adapter_ram // (1024 * 1024) if adapter_ram > 0 else 0
                    # WMI AdapterRAM is a 32-bit uint that caps at 4095 MB on >=4GB GPUs
                    if vram_mb <= 0 or vram_mb >= 4095:
                        vram_mb = max(vram_mb, 8192)
                    return {
                        'available': True,
                        'acceleration': 'cuda',
                        'name': name,
                        'vram_mb': vram_mb,
                        'count': 1,
                        'compute_capability': '',
                        'cuda_arch': '',
                        'driver_version': str(ctrl.get('DriverVersion') or '').strip(),
                    }
    except Exception:
        pass
    return None


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
    smi_bin = _find_nvidia_smi()
    nvcc_bin = _find_nvcc()
    if smi_bin or nvcc_bin:
        if smi_bin:
            try:
                res = subprocess.run([smi_bin, '-L'], capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and 'GPU' in res.stdout:
                    return 'cuda'
            except Exception:
                pass
        if nvcc_bin:
            return 'cuda'
    if plat == 'windows' and _detect_windows_gpu_wmi():
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

    smi_bin = _find_nvidia_smi()
    if smi_bin:
        try:
            res = subprocess.run(
                [smi_bin, '--query-gpu=name,memory.total,compute_cap,driver_version', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=5
            )
            has_cc_col = True
            if res.returncode != 0 or not res.stdout.strip():
                has_cc_col = False
                res = subprocess.run(
                    [smi_bin, '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader,nounits'],
                    capture_output=True, text=True, timeout=5
                )
            if res.returncode == 0 and res.stdout.strip():
                lines = [ln.strip() for ln in res.stdout.strip().splitlines() if ln.strip()]
                parts = [x.strip() for x in lines[0].split(',')]
                name = parts[0] if len(parts) > 0 and parts[0] else 'NVIDIA GPU'
                vram = 0
                if len(parts) > 1:
                    m_vram = re.search(r'(\d+(?:\.\d+)?)', parts[1])
                    if m_vram:
                        vram = int(float(m_vram.group(1)))
                if has_cc_col:
                    raw_cc = parts[2] if len(parts) > 2 else ''
                    drv = parts[3] if len(parts) > 3 else ''
                else:
                    raw_cc = ''
                    drv = parts[2] if len(parts) > 2 else ''
                cc = raw_cc if re.match(r'^\d+\.\d+$', raw_cc) else ''
                cuda_arch = cc.replace('.', '') if cc else ''
                info.update({
                    'available': True,
                    'acceleration': 'cuda',
                    'name': name,
                    'vram_mb': vram if vram > 0 else 8192,
                    'count': len(lines),
                    'compute_capability': cc,
                    'cuda_arch': cuda_arch,
                    'driver_version': drv,
                })
                return info
        except Exception:
            pass

    if plat == 'windows':
        wmi_gpu = _detect_windows_gpu_wmi()
        if wmi_gpu:
            info.update(wmi_gpu)
            return info
    return info


def scan_and_save_hardware() -> Dict[str, Any]:
    """
    Scans system CPU, RAM, GPU, and installed build packages, computes optimal
    compilation flags and runtime defaults for llama.cpp, koboldcpp, and ollama,
    and writes the profile to `config/hardware.json`.
    """
    plat = get_platform_name()
    cpu_cores = os.cpu_count() or 4
    p_cores = _detect_apple_silicon_p_cores() if plat == 'macos' else None
    ram_mb = _detect_ram_mb()
    gpu = _detect_gpu_details()
    has_cc, cc_detail = check_c_compiler()
    nvcc_path = _find_nvcc()

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

    # Auto-tune runtime settings based on detected VRAM / RAM & CPU cores (using P-cores on Apple Silicon)
    vram_mb = gpu.get('vram_mb', 0)
    if p_cores:
        opt_threads = max(2, min(32, p_cores))
    else:
        opt_threads = max(2, min(32, cpu_cores // 2 if cpu_cores >= 8 else max(2, cpu_cores - 1)))
    opt_threads_batch = max(opt_threads, min(64, cpu_cores - 2 if cpu_cores >= 8 else cpu_cores))

    if gpu['available']:
        if vram_mb >= 20000:      # 24GB+ (e.g. RTX 3090/4090/5090 or 32GB+ Mac)
            opt_layers = 99
            opt_context = 32768
            opt_parallel = 4
            opt_batch = 4096
            opt_ubatch = 512
        elif vram_mb >= 11000:    # 12GB - 16GB
            opt_layers = 48
            opt_context = 16384
            opt_parallel = 2
            opt_batch = 2048
            opt_ubatch = 512
        elif vram_mb >= 7000:     # 8GB
            opt_layers = 32
            opt_context = 16384
            opt_parallel = 2
            opt_batch = 2048
            opt_ubatch = 256
        else:                     # <= 6GB (e.g. 3GB - 4GB GTX 1050)
            opt_layers = 8
            opt_context = 8192
            opt_parallel = 1
            opt_batch = 2048
            opt_ubatch = 256
        opt_flash_attn = 'on'
    else:
        opt_layers = 0
        opt_context = 8192 if ram_mb >= 16000 else 4096
        opt_parallel = 1
        opt_batch = 1024
        opt_ubatch = 256
        opt_flash_attn = 'off'

    # Auto-tune build flags for llama.cpp, koboldcpp, and ollama
    parallel_jobs = max(2, min(16, cpu_cores - 1 if cpu_cores > 2 else 2))
    llama_cmake_flags = [
        '-DCMAKE_BUILD_TYPE=Release',
        '-DGGML_NATIVE=ON',
        '-DCMAKE_C_FLAGS=-O3 -march=native -mtune=native',
        '-DCMAKE_CXX_FLAGS=-O3 -march=native -mtune=native',
    ]
    if gpu['acceleration'] == 'cuda' and nvcc_path:
        llama_cmake_flags.extend([
            '-DGGML_CUDA=ON',
            f'-DCMAKE_CUDA_COMPILER={nvcc_path}',
            '-DGGML_CUDA_FA_ALL_QUANTS=ON',
            '-DCMAKE_CUDA_FLAGS=-O3 -use_fast_math',
        ])
        cc_str = gpu.get('compute_capability', '')
        try:
            cc_float = float(cc_str)
        except (ValueError, TypeError):
            cc_float = 0.0
        # Pascal / GTX 1000 (< 7.0) lacks Tensor Cores: force MMQ with DP4A integer instructions
        if 0 < cc_float < 7.0:
            llama_cmake_flags.append('-DGGML_CUDA_FORCE_MMQ=ON')
        # Turing / Ampere / Ada / Blackwell (>= 7.0): omit FORCE_MMQ so cuBLAS utilizes native Tensor Cores

        if gpu.get('cuda_arch'):
            llama_cmake_flags.append(f"-DCMAKE_CUDA_ARCHITECTURES={gpu['cuda_arch']}")
    elif gpu['acceleration'] == 'metal':
        llama_cmake_flags.append('-DGGML_METAL=ON')

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
            'parallel': opt_parallel,
            'threads': opt_threads,
            'threads_batch': opt_threads_batch,
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
            'nodejs': ['winget', 'install', '--id', 'OpenJS.NodeJS.LTS', '-e', '--source', 'winget'],
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
            'nodejs': ['brew', 'install', 'node'],
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
                'nodejs': 'nodejs npm',
            }
            pkg = pkg_map.get(tool)
            if not pkg:
                return []
            pkgs = pkg.split()
            return ['sudo', 'apt-get', 'install', '-y'] + pkgs
        elif shutil.which('dnf'):
            pkg_map = {
                'git': 'git',
                'compiler': 'gcc-c++',
                'cmake': 'cmake',
                'ninja': 'ninja-build',
                'make': 'make',
                'go': 'golang',
                'cuda': 'cuda-toolkit',
                'nodejs': 'nodejs npm',
            }
            pkg = pkg_map.get(tool)
            if not pkg:
                return []
            pkgs = pkg.split()
            return ['sudo', 'dnf', 'install', '-y'] + pkgs
        elif shutil.which('pacman'):
            pkg_map = {
                'git': 'git',
                'compiler': 'base-devel',
                'cmake': 'cmake',
                'ninja': 'ninja',
                'make': 'make',
                'go': 'go',
                'cuda': 'cuda',
                'nodejs': 'nodejs npm',
            }
            pkg = pkg_map.get(tool)
            if not pkg:
                return []
            pkgs = pkg.split()
            return ['sudo', 'pacman', '-S', '--noconfirm'] + pkgs
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
        nvcc_bin = _find_nvcc()
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

    target_bin = build_dir / 'bin' / get_executable_name('llama-server')
    if not target_bin.is_file() and (build_dir / 'CMakeCache.txt').is_file():
        shutil.rmtree(build_dir, ignore_errors=True)

    cmake_args = ['cmake', '-S', str(src_dir), '-B', str(build_dir)]
    if shutil.which('ninja'):
        cmake_args.extend(['-G', 'Ninja'])
    cmake_args.extend(cmake_flags)

    jobs = str(hw.get('build_config', {}).get('parallel_jobs', 4))
    build_cmd = ['cmake', '--build', str(build_dir), '--config', 'Release', '--target', 'llama-server', '-j', jobs]

    print(f"Compiling llama.cpp with hardware flags: {' '.join(cmake_flags)} (-j {jobs})")
    env = dict(os.environ)
    nvcc_bin = _find_nvcc()
    if nvcc_bin:
        nvcc_dir = str(Path(nvcc_bin).parent)
        env['PATH'] = nvcc_dir + os.pathsep + env.get('PATH', '')
    try:
        if os.name == 'nt':
            msvc = find_windows_msvc()
            if msvc and msvc != 'PATH':
                cfg_line = f'call "{msvc}" && ' + ' '.join(f'"{a}"' if ' ' in a else a for a in cmake_args)
                bld_line = f'call "{msvc}" && ' + ' '.join(f'"{a}"' if ' ' in a else a for a in build_cmd)
                subprocess.run(cfg_line, shell=True, check=True, env=env)
                subprocess.run(bld_line, shell=True, check=True, env=env)
            else:
                subprocess.run(cmake_args, check=True, env=env)
                subprocess.run(build_cmd, check=True, env=env)
        else:
            subprocess.run(cmake_args, check=True, env=env)
            subprocess.run(build_cmd, check=True, env=env)
        print(f"llama.cpp compiled successfully in {build_dir}")
        return True
    except Exception as e:
        print(f"Error compiling llama.cpp: {e}")
        return False


def download_prebuilt_llama_cpp(accel: str) -> bool:
    """Downloads official prebuilt llama.cpp release archive from GitHub for Windows, macOS, or Linux.
    On Windows with CUDA, downloads BOTH the llama CUDA binary archive AND the mandatory
    cudart-llama companion archive so cublas64/cudart64 DLLs are present alongside llama-server.exe.
    On Linux, prefers CUDA/Vulkan prebuilt archives when accel == 'cuda', falling back to standard x64.
    On macOS, strips Gatekeeper com.apple.quarantine attributes after extraction.
    """
    plat = get_platform_name()
    dest_dir = BIN_DIR / plat
    dest_dir.mkdir(parents=True, exist_ok=True)

    api_url = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    req = urllib.request.Request(api_url, headers={'User-Agent': 'llmCode-Installer/1.0'})
    release_data = {}
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            release_data = json.loads(r.read())
    except Exception as e:
        print(f"Warning: Could not query llama.cpp GitHub API: {e}")

    assets = release_data.get('assets', [])
    target_asset = None
    cudart_asset = None
    linux_fallback_asset = None

    for asset in assets:
        name = asset.get('name', '').lower()
        if plat == 'windows' and ('bin-win' in name or '-win' in name) and ('x64' in name or 'amd64' in name):
            if accel == 'cuda' and ('cuda' in name or 'cu12' in name or 'cu11' in name):
                if name.startswith('cudart-'):
                    if not cudart_asset:
                        cudart_asset = asset
                elif not target_asset:
                    target_asset = asset
            elif accel != 'cuda' and not name.startswith('cudart-') and ('avx2' in name or 'cpu' in name or 'vulkan' in name or 'x64' in name):
                if not target_asset:
                    target_asset = asset
        elif plat == 'macos' and ('bin-macos' in name or '-macos' in name or '-darwin' in name):
            arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64'
            if arch in name and not target_asset:
                target_asset = asset
        elif plat == 'linux' and ('bin-ubuntu' in name or 'bin-linux' in name or '-linux' in name) and ('x64' in name or 'amd64' in name):
            if accel == 'cuda' and ('cuda' in name or 'vulkan' in name):
                if not target_asset:
                    target_asset = asset
            elif not linux_fallback_asset:
                linux_fallback_asset = asset

    if plat == 'linux' and not target_asset and linux_fallback_asset:
        target_asset = linux_fallback_asset

    if not target_asset:
        # Fallback to direct well-known GitHub release artifact URLs
        if plat == 'windows':
            asset_name = 'llama-b4850-bin-win-cuda-cu12.4-x64.zip' if accel == 'cuda' else 'llama-b4850-bin-win-avx2-x64.zip'
        elif plat == 'macos':
            asset_name = 'llama-b4850-bin-macos-arm64.zip' if platform.machine().lower() in ('arm64', 'aarch64') else 'llama-b4850-bin-macos-x64.zip'
        else:
            asset_name = 'llama-b4850-bin-ubuntu-x64.zip'
        target_asset = {
            'name': asset_name,
            'browser_download_url': f"https://github.com/ggml-org/llama.cpp/releases/latest/download/{asset_name}"
        }

    if plat == 'windows' and accel == 'cuda' and not cudart_asset:
        cudart_name = 'cudart-llama-bin-win-cuda-12.4-x64.zip'
        cudart_asset = {
            'name': cudart_name,
            'browser_download_url': f"https://github.com/ggml-org/llama.cpp/releases/latest/download/{cudart_name}"
        }

    # Clean stale DLLs/binaries in dest_dir before extracting so switching CPU -> CUDA replaces ggml DLLs cleanly
    for stale in list(dest_dir.glob('*.dll')) + list(dest_dir.glob('*.so*')) + list(dest_dir.glob('*.dylib')) + list(dest_dir.glob('llama-*')):
        if stale.is_file() or stale.is_symlink():
            stale.unlink(missing_ok=True)

    def _download_and_extract(asset_obj: Dict[str, Any]) -> bool:
        archive_path = dest_dir / asset_obj['name']
        try:
            download_file(asset_obj['browser_download_url'], archive_path)
        except Exception as e:
            print(f"Failed to download {asset_obj['browser_download_url']}: {e}")
            return False
        try:
            if archive_path.name.endswith('.zip'):
                with zipfile.ZipFile(archive_path, 'r') as zf:
                    zf.extractall(dest_dir)
            elif archive_path.name.endswith(('.tar.gz', '.tgz')):
                with tarfile.open(archive_path, 'r:gz') as tf:
                    tf.extractall(dest_dir)
            return True
        finally:
            archive_path.unlink(missing_ok=True)

    if cudart_asset:
        print(f"Downloading companion CUDA runtime DLLs ({cudart_asset['name']})...")
        _download_and_extract(cudart_asset)

    if not _download_and_extract(target_asset):
        return False

    if os.name != 'nt':
        for pattern in ('llama-*', '*.so*', '*.dylib'):
            for p in dest_dir.rglob(pattern):
                if p.is_file() and not p.is_symlink():
                    try:
                        p.chmod(0o755)
                    except Exception:
                        pass
    _strip_macos_quarantine(dest_dir)

    (dest_dir / 'llama_cpp.flavor').write_text(accel + '\n', encoding='utf-8')
    print(f"Prebuilt llama.cpp ({accel}) installed in {dest_dir}")
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

    env = dict(os.environ)
    nvcc_bin = _find_nvcc()
    if nvcc_bin:
        nvcc_dir = str(Path(nvcc_bin).parent)
        env['PATH'] = nvcc_dir + os.pathsep + env.get('PATH', '')

    plat = get_platform_name()
    if plat == 'windows':
        build_dir = src_dir / 'build'
        if (build_dir / 'CMakeCache.txt').is_file():
            shutil.rmtree(build_dir, ignore_errors=True)

        cmake_args = ['cmake', '-S', str(src_dir), '-B', str(build_dir), '-DCMAKE_BUILD_TYPE=Release']
        if shutil.which('ninja'):
            cmake_args.extend(['-G', 'Ninja'])
        if accel == 'cuda':
            cmake_args.append('-DLLAMA_CUBLAS=ON')
            if nvcc_bin:
                cmake_args.append(f'-DCMAKE_CUDA_COMPILER={nvcc_bin}')

        jobs = str(hw.get('build_config', {}).get('parallel_jobs', 4))
        build_cmd = ['cmake', '--build', str(build_dir), '--config', 'Release', '-j', jobs]

        try:
            msvc = find_windows_msvc()
            if msvc and msvc != 'PATH':
                cfg_line = f'call "{msvc}" && ' + ' '.join(f'"{a}"' if ' ' in a else a for a in cmake_args)
                bld_line = f'call "{msvc}" && ' + ' '.join(f'"{a}"' if ' ' in a else a for a in build_cmd)
                subprocess.run(cfg_line, shell=True, check=True, env=env)
                subprocess.run(bld_line, shell=True, check=True, env=env)
            else:
                subprocess.run(cmake_args, check=True, env=env)
                subprocess.run(build_cmd, check=True, env=env)
            print(f"KoboldCpp compiled successfully in {src_dir}")
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
            subprocess.run(make_cmd, check=True, env=env)
            print(f"KoboldCpp compiled successfully in {src_dir}")
            return True
        except Exception as e:
            print(f"Error compiling KoboldCpp: {e}")
            return False


def download_prebuilt_koboldcpp(accel: str) -> bool:
    """Downloads official standalone KoboldCpp binary from GitHub releases into bin/<platform>/ and bin/."""
    plat = get_platform_name()
    exe_name = get_executable_name('koboldcpp')
    plat_dir = BIN_DIR / plat
    plat_dir.mkdir(parents=True, exist_ok=True)
    dest = plat_dir / exe_name
    legacy_dest = BIN_DIR / exe_name

    if plat == 'windows':
        asset_name = 'koboldcpp.exe' if accel == 'cuda' else 'koboldcpp_nocuda.exe'
    elif plat == 'macos':
        asset_name = 'koboldcpp-mac-arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'koboldcpp-mac-x64'
    else:
        asset_name = 'koboldcpp-linux-x64-cuda1210' if accel == 'cuda' else 'koboldcpp-linux-x64-nocuda'

    url = f"https://github.com/LostRuins/koboldcpp/releases/latest/download/{asset_name}"
    try:
        if legacy_dest.is_file() or legacy_dest.is_symlink():
            legacy_dest.unlink(missing_ok=True)
        download_file(url, dest)
        if os.name != 'nt':
            dest.chmod(0o755)
        _strip_macos_quarantine(dest)
        (plat_dir / 'koboldcpp.flavor').write_text(accel + '\n', encoding='utf-8')
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

    env = dict(os.environ)
    nvcc_bin = _find_nvcc()
    if nvcc_bin:
        nvcc_dir = str(Path(nvcc_bin).parent)
        env['PATH'] = nvcc_dir + os.pathsep + env.get('PATH', '')

    exe_name = get_executable_name('ollama')
    jobs = str(hw.get('build_config', {}).get('parallel_jobs', 4))
    try:
        if (src_dir / 'CMakeLists.txt').is_file() and shutil.which('cmake'):
            cmake_cmd = ['cmake', '-S', str(src_dir), '-B', str(src_dir / 'build'), '-DCMAKE_BUILD_TYPE=Release']
            if nvcc_bin:
                cmake_cmd.append(f"-DCMAKE_CUDA_COMPILER={nvcc_bin}")
            if hw['gpu'].get('cuda_arch'):
                cmake_cmd.append(f"-DCMAKE_CUDA_ARCHITECTURES={hw['gpu']['cuda_arch']}")
            subprocess.run(cmake_cmd, check=True, env=env)
            subprocess.run(['cmake', '--build', str(src_dir / 'build'), '-j', jobs], check=True, env=env)
        subprocess.run(['go', 'build', '-o', str(src_dir / exe_name), '.'], cwd=str(src_dir), check=True, env=env)
        print(f"Ollama compiled successfully at {src_dir / exe_name}")
        return True
    except Exception as e:
        print(f"Error compiling Ollama from source: {e}")
        return False


def download_prebuilt_ollama() -> bool:
    """Downloads official prebuilt Ollama binary distribution into bin/<platform>/ and bin/ollama_dist."""
    plat = get_platform_name()
    dist_dir = BIN_DIR / 'ollama_dist'
    dist_dir.mkdir(parents=True, exist_ok=True)
    plat_dir = BIN_DIR / plat
    plat_dir.mkdir(parents=True, exist_ok=True)

    if plat == 'windows':
        url = "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip"
        archive = dist_dir / "ollama-windows-amd64.zip"
        try:
            download_file(url, archive)
            with zipfile.ZipFile(archive, 'r') as zf:
                zf.extractall(plat_dir)
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
                tf.extractall(dist_dir)
            archive.unlink(missing_ok=True)
            extracted_bin = dist_dir / 'ollama'
            if not extracted_bin.is_file():
                app_bin = dist_dir / 'Ollama.app' / 'Contents' / 'Resources' / 'ollama'
                if app_bin.is_file():
                    extracted_bin = app_bin
            target_bin = plat_dir / 'ollama'
            if extracted_bin.is_file():
                shutil.copy2(extracted_bin, target_bin)
                target_bin.chmod(0o755)
                _strip_macos_quarantine(target_bin)
                link_path = BIN_DIR / 'ollama'
                if link_path.exists() or link_path.is_symlink():
                    link_path.unlink(missing_ok=True)
                link_path.symlink_to(target_bin)
                return True
            return False
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
                for link_path in (BIN_DIR / 'ollama', plat_dir / 'ollama'):
                    if link_path.exists() or link_path.is_symlink():
                        link_path.unlink(missing_ok=True)
                    link_path.symlink_to(bin_path)
            return True
        except Exception as e:
            print(f"Failed to download Linux Ollama: {e}")
            return False


# ==============================================================================
# CLI Agents: OpenCode, Claude Code & Codex (Installed directly inside llm-dash .venv)
# ==============================================================================
def is_binary_runnable(bin_path: Path, min_version: Tuple[int, int, int] = (0, 0, 0)) -> bool:
    """Verifies that an executable binary can run and optionally meets min_version."""
    try:
        target_str = str(bin_path)
        if os.name == 'nt' and target_str.lower().endswith(('.cmd', '.bat')):
            res = subprocess.run(['cmd.exe', '/c', target_str, '--version'], capture_output=True, text=True, timeout=5)
        else:
            res = subprocess.run([target_str, '--version'], capture_output=True, text=True, timeout=5)
        if res.returncode != 0:
            return False
        if min_version > (0, 0, 0):
            ver_out = (res.stdout or '') + (res.stderr or '')
            ver = parse_version_tuple(ver_out)
            if ver != (0, 0, 0) and ver < min_version:
                return False
        return True
    except Exception:
        return False


def _find_existing_harness(name: str, venv_dir: Path) -> Optional[Tuple[Path, Tuple[int, int, int], str]]:
    """
    Checks if a compatible harness binary (`claude`, `codex`, `opencode`) already exists on the system outside .venv.
    Returns (binary_path, version_tuple, version_str) if found and meeting HARNESS_MIN_VERSIONS floor; else None.
    """
    from src.common.paths import HARNESS_MIN_VERSIONS
    floor = HARNESS_MIN_VERSIONS.get(name, (0, 0, 0))
    resolved_venv = venv_dir.resolve()

    candidates: List[Path] = []
    which_bin = shutil.which(name)
    if which_bin:
        candidates.append(Path(which_bin))

    # Check common system bin locations if missing from current shell PATH
    if os.name != 'nt':
        home = Path.home()
        for extra in [
            home / '.local' / 'bin' / name,
            home / '.npm-global' / 'bin' / name,
            Path('/usr/local/bin') / name,
        ]:
            if extra.is_file() and extra not in candidates:
                candidates.append(extra)
    else:
        appdata = os.environ.get('APPDATA')
        if appdata:
            npm_win = Path(appdata) / 'npm' / f'{name}.cmd'
            if npm_win.is_file() and npm_win not in candidates:
                candidates.append(npm_win)

    for cand in candidates:
        try:
            cand_resolved = cand.resolve()
            if resolved_venv in cand_resolved.parents or resolved_venv == cand_resolved:
                continue

            target_str = str(cand_resolved) if cand.suffix.lower() not in ('.cmd', '.bat') else str(cand)
            argv = ['cmd.exe', '/c', target_str, '--version'] if os.name == 'nt' and target_str.lower().endswith(('.cmd', '.bat')) else [target_str, '--version']
            res = subprocess.run(argv, capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                raw_out = (res.stdout or '') + (res.stderr or '')
                ver = parse_version_tuple(raw_out)
                if ver != (0, 0, 0) and ver >= floor:
                    first_line = raw_out.strip().splitlines()[0] if raw_out.strip() else f"v{ver[0]}.{ver[1]}.{ver[2]}"
                    return cand, ver, first_line
        except Exception:
            continue
    return None


def _link_existing_harness_into_venv(venv_dir: Path, name: str, existing_bin: Path) -> bool:
    """
    Creates a symlink or executable wrapper shim inside `<venv_dir>/bin` or `<venv_dir>/Scripts`
    pointing to `existing_bin`, avoiding duplicate npm installations.
    """
    from src.common.paths import HARNESS_MIN_VERSIONS
    floor = HARNESS_MIN_VERSIONS.get(name, (0, 0, 0))
    is_win = os.name == 'nt' or get_platform_name() == 'windows'
    venv_bin_dir = venv_dir / ('Scripts' if is_win else 'bin')
    venv_bin_dir.mkdir(parents=True, exist_ok=True)

    target = venv_bin_dir / (f'{name}.cmd' if is_win else name)
    target.unlink(missing_ok=True)

    if is_win:
        resolved_str = str(existing_bin.resolve() if existing_bin.is_file() else existing_bin)
        target.write_text(f'@echo off\r\n"{resolved_str}" %*\r\n', encoding='utf-8')
    else:
        try:
            target.symlink_to(existing_bin)
        except OSError:
            target.write_text(f'#!/usr/bin/env bash\nexec "{existing_bin.resolve()}" "$@"\n', encoding='utf-8')
            target.chmod(0o755)

    return is_binary_runnable(target, min_version=floor)


def _install_opencode_into_venv(venv_dir: Path, non_interactive: bool = False) -> bool:
    """
    Installs OpenCode v2 CLI (`@opencode/cli@latest`, >= 2.0.0 architecture) directly inside the
    llm-dash virtualenv (`.venv/bin/opencode` on Linux/macOS or `.venv/Scripts/opencode.exe` on Windows).
    Purges legacy v1 (`~/.opencode`, `opencode-ai` 1.x, or `< 2.0.0`) binaries and prioritizes npm `@opencode/cli@latest`.
    """
    plat = get_platform_name()
    is_win = (os.name == 'nt' or plat == 'windows')
    venv_bin_dir = venv_dir / ('Scripts' if is_win else 'bin')
    venv_bin_dir.mkdir(parents=True, exist_ok=True)
    exe_name = get_executable_name('opencode')
    target_bin = venv_bin_dir / exe_name
    win_cmd_wrapper = venv_bin_dir / 'opencode.cmd'

    # Purge legacy v1 ~/.opencode installation directory and stale v1 node_modules if present
    for legacy_dir in (
        Path.home() / '.opencode',
        Path.home() / '.config' / 'opencode' / 'node_modules' / '@opencode-ai',
        venv_dir / 'node_modules' / 'opencode-ai',
        venv_dir / 'node_modules' / '@opencode-ai',
        venv_dir / 'node_modules' / 'opencode-linux-x64',
        venv_dir / 'node_modules' / 'opencode-linux-x64-baseline',
        venv_dir / 'node_modules' / 'opencode-linux-arm64',
        venv_dir / 'node_modules' / 'opencode-darwin-arm64',
        venv_dir / 'node_modules' / 'opencode-darwin-x64',
        venv_dir / 'node_modules' / 'opencode-darwin-x64-baseline',
        venv_dir / 'node_modules' / 'opencode-windows-x64',
        venv_dir / 'node_modules' / 'opencode-windows-x64-baseline',
        venv_dir / 'node_modules' / 'opencode-windows-arm64',
    ):
        if legacy_dir.is_dir():
            shutil.rmtree(legacy_dir, ignore_errors=True)

    # Purge cached npx v1 opencode-ai directories across Linux/macOS (~/.npm/_npx) and Windows (LocalAppData/npm-cache/_npx)
    npx_roots = [Path.home() / '.npm' / '_npx']
    local_appdata = os.environ.get('LOCALAPPDATA')
    if local_appdata:
        npx_roots.append(Path(local_appdata) / 'npm-cache' / '_npx')
    for npx_root in npx_roots:
        if npx_root.is_dir():
            for sub in npx_root.iterdir():
                if (sub / 'node_modules' / 'opencode-ai').is_dir():
                    shutil.rmtree(sub, ignore_errors=True)

    # Also clean ~/.config/opencode/package.json if it pins legacy @opencode-ai/plugin 1.x
    cfg_pkg = Path.home() / '.config' / 'opencode' / 'package.json'
    if cfg_pkg.is_file():
        try:
            txt = cfg_pkg.read_text(encoding='utf-8')
            if '@opencode-ai/plugin' in txt:
                cfg_pkg.unlink(missing_ok=True)
                (Path.home() / '.config' / 'opencode' / 'package-lock.json').unlink(missing_ok=True)
                shutil.rmtree(Path.home() / '.config' / 'opencode' / 'node_modules', ignore_errors=True)
        except Exception:
            pass

    for existing_candidate in (target_bin, win_cmd_wrapper):
        if existing_candidate.is_file() or existing_candidate.is_symlink():
            if is_binary_runnable(existing_candidate, min_version=(2, 0, 0)):
                print(f"OpenCode v2 CLI already present and runnable in venv at {existing_candidate}")
                return True
            print(f"Existing OpenCode binary at {existing_candidate} is legacy v1 (< 2.0.0) or failed check; upgrading to OpenCode v2...")
            existing_candidate.unlink(missing_ok=True)

    # Check for existing system OpenCode installation outside .venv
    existing_sys = _find_existing_harness('opencode', venv_dir)
    if existing_sys:
        sys_bin, sys_ver, sys_str = existing_sys
        print(f"\nFound existing OpenCode v2 CLI on system: {sys_bin} ({sys_str})")
        ans = _prompt_choice(
            "Link existing OpenCode v2 to avoid re-downloading via npm? (y/n)",
            ['y', 'yes', 'n', 'no'],
            default='y',
            non_interactive=non_interactive,
        )
        if ans in ('y', 'yes'):
            if _link_existing_harness_into_venv(venv_dir, 'opencode', sys_bin):
                print(f"Linked existing OpenCode v2 CLI into venv.")
                return True
            print("Linking failed; falling back to fresh installation...")

    # 1. Primary: install OpenCode v2 via npm scoped to venv_dir (`npm install --prefix <venv_dir> @opencode/cli@latest`)
    npm_exe = shutil.which('npm')
    if npm_exe:
        print(f"Installing OpenCode v2 CLI into {venv_dir} via npm (@opencode/cli@latest)...")
        try:
            subprocess.run([npm_exe, 'install', '--prefix', str(venv_dir), '@opencode/cli@latest'], check=True)
            npm_bin = venv_dir / 'node_modules' / '.bin' / ('opencode.cmd' if is_win else 'opencode')
            installed_pkg_bin = venv_dir / 'node_modules' / '@opencode' / 'cli' / 'bin' / 'opencode.exe'
            if not installed_pkg_bin.is_file():
                installed_pkg_bin = venv_dir / 'node_modules' / '@opencode' / 'cli' / 'bin' / exe_name

            if is_win:
                # On Windows, installed_pkg_bin is the native PE executable (node_modules\@opencode\cli\bin\opencode.exe).
                # Copy it directly to .venv\Scripts\opencode.exe and write a clean opencode.cmd wrapper pointing to it.
                if installed_pkg_bin.is_file():
                    shutil.copy2(installed_pkg_bin, target_bin)
                    win_cmd_wrapper.write_text(f'@echo off\r\n"{target_bin}" %*\r\n', encoding='utf-8')
                    check_target = target_bin
                elif npm_bin.is_file():
                    check_target = npm_bin
                else:
                    check_target = None
            else:
                src_bin = npm_bin if (npm_bin.is_file() or npm_bin.is_symlink()) else (installed_pkg_bin if installed_pkg_bin.is_file() else None)
                if src_bin:
                    if target_bin.exists() or target_bin.is_symlink():
                        target_bin.unlink(missing_ok=True)
                    target_bin.symlink_to(src_bin)
                    try:
                        resolved_bin = src_bin.resolve()
                        resolved_bin.chmod(0o755)
                        _strip_macos_quarantine(resolved_bin)
                    except Exception:
                        pass
                    _strip_macos_quarantine(venv_dir / 'node_modules' / '@opencode')
                    check_target = target_bin
                else:
                    check_target = None

            if check_target and is_binary_runnable(check_target, min_version=(2, 0, 0)):
                print(f"OpenCode v2 installed and verified in venv via npm ({check_target}).")
                return True
        except Exception as e:
            print(f"Warning: venv npm install failed: {e}")

    # 2. Secondary fallback: download official standalone v2 binary release from GitHub
    api_url = "https://api.github.com/repos/anomalyco/opencode/releases"
    req = urllib.request.Request(api_url, headers={'User-Agent': 'llmCode-Installer/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            releases = json.loads(r.read())
        rel = None
        if isinstance(releases, list):
            for candidate_rel in releases:
                tag = str(candidate_rel.get('tag_name', ''))
                if parse_version_tuple(tag) >= (2, 0, 0):
                    rel = candidate_rel
                    break
            if not rel and releases:
                rel = releases[0]
        elif isinstance(releases, dict):
            rel = releases

        arch_is_arm = platform.machine().lower() in ('arm64', 'aarch64')
        arch_tok = 'arm64' if arch_is_arm else 'x64'
        plat_tok = 'windows' if plat == 'windows' else ('darwin' if plat == 'macos' else 'linux')

        is_musl = False
        if plat == 'linux':
            is_musl = any(Path(p).is_file() for p in ['/lib/ld-musl-x86_64.so.1', '/lib/ld-musl-aarch64.so.1'])

        candidates = []
        for asset in (rel or {}).get('assets', []):
            aname = asset.get('name', '').lower()
            if not aname.endswith(('.zip', '.tar.gz', '.tgz')):
                continue
            if plat_tok not in aname or arch_tok not in aname:
                continue
            if any(k in aname for k in ('desktop', '.deb', '.rpm', '.appimage')):
                continue
            score = 10
            if is_musl and 'musl' in aname:
                score += 20
            elif not is_musl and 'musl' in aname:
                score -= 100  # Avoid musl on glibc systems
            if 'baseline' in aname:
                score -= 10
            if aname in (f"opencode-{plat_tok}-{arch_tok}.tar.gz", f"opencode-{plat_tok}-{arch_tok}.zip"):
                score += 50
            candidates.append((score, asset))

        candidates.sort(key=lambda x: x[0], reverse=True)
        matched_asset = candidates[0][1] if candidates and candidates[0][0] > 0 else None

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
                    _strip_macos_quarantine(target_bin)
                    break
            shutil.rmtree(extract_tmp, ignore_errors=True)
            if target_bin.is_file() and is_binary_runnable(target_bin, min_version=(2, 0, 0)):
                print(f"OpenCode v2 installed into venv at: {target_bin}")
                return True
    except Exception:
        pass

    print("Manual guidance for OpenCode v2: Ensure Node.js & npm (https://nodejs.org) are installed (`npm install -g @opencode/cli@latest`).")
    return False


# npm package per npm-scoped harness; floors live in src.common.paths.HARNESS_MIN_VERSIONS.
NPM_HARNESS_PACKAGES = {
    'claude': '@anthropic-ai/claude-code',
    'codex': '@openai/codex',
}


def _install_npm_harness(venv_dir: Path, name: str, non_interactive: bool = False) -> bool:
    """
    Installs an npm harness CLI (`claude`, `codex`) scoped to the llm-dash .venv (`npm install --prefix <venv>`)
    or links to an existing system installation, and exposes it as `.venv/bin/<name>` (symlink)
    or `.venv/Scripts/<name>.cmd` (wrapper) so discovery and PATH-free launches work identically to OpenCode.
    """
    from src.common.paths import HARNESS_MIN_VERSIONS
    pkg = NPM_HARNESS_PACKAGES[name]
    floor = HARNESS_MIN_VERSIONS.get(name, (0, 0, 0))
    is_win = os.name == 'nt'
    venv_bin_dir = venv_dir / ('Scripts' if is_win else 'bin')
    venv_bin_dir.mkdir(parents=True, exist_ok=True)
    target = venv_bin_dir / (f'{name}.cmd' if is_win else name)
    if (target.is_file() or target.is_symlink()) and is_binary_runnable(target, min_version=floor):
        print(f"{name} CLI already present and runnable in venv at {target}")
        return True

    # Check for existing system installation outside .venv
    existing_sys = _find_existing_harness(name, venv_dir)
    if existing_sys:
        sys_bin, sys_ver, sys_str = existing_sys
        print(f"\nFound existing {name} CLI on system: {sys_bin} ({sys_str})")
        ans = _prompt_choice(
            f"Link existing {name} to avoid re-downloading via npm? (y/n)",
            ['y', 'yes', 'n', 'no'],
            default='y',
            non_interactive=non_interactive,
        )
        if ans in ('y', 'yes'):
            if _link_existing_harness_into_venv(venv_dir, name, sys_bin):
                print(f"Linked existing {name} CLI into venv at {target}.")
                return True
            print("Linking failed; falling back to fresh installation...")

    npm_exe = shutil.which('npm')
    if not npm_exe:
        print(f"npm not found; install Node.js >= 18 (https://nodejs.org) then rerun setup to get {pkg}.")
        return False
    print(f"Installing {pkg}@latest into {venv_dir} via npm...")
    try:
        subprocess.run([npm_exe, 'install', '--prefix', str(venv_dir), f'{pkg}@latest'], check=True)
    except Exception as e:
        print(f"Warning: npm install of {pkg} failed: {e}")
        return False
    npm_bin = venv_dir / 'node_modules' / '.bin' / (f'{name}.cmd' if is_win else name)
    if not (npm_bin.is_file() or npm_bin.is_symlink()):
        print(f"Warning: {npm_bin} missing after npm install.")
        return False
    target.unlink(missing_ok=True)
    if is_win:
        target.write_text(f'@echo off\r\n"{npm_bin.resolve()}" %*\r\n', encoding='utf-8')
    else:
        target.symlink_to(npm_bin.resolve())
        _strip_macos_quarantine(venv_dir / 'node_modules' / pkg.split('/')[0])
    ok = is_binary_runnable(target, min_version=floor)
    print(f"{name} CLI {'installed and verified' if ok else 'installed but failed version check'} at {target}")
    return ok


def _purge_aider(venv_python: str, venv_dir: Path) -> None:
    """Removes the retired Aider harness (aider-chat) from existing .venv installs; litellm is kept for the proxy fallback."""
    aider_bin = venv_dir / ('Scripts' if os.name == 'nt' else 'bin') / get_executable_name('aider')
    if not aider_bin.exists():
        return
    print("Removing retired Aider harness (aider-chat) from llm-dash .venv...")
    subprocess.run([venv_python, '-m', 'pip', 'uninstall', '-y', '-q', 'aider-chat'], check=False)
    aider_bin.unlink(missing_ok=True)


def _ensure_npm_available(non_interactive: bool = False) -> Optional[str]:
    """Ensures npm is available, offering package manager auto-install if missing."""
    npm_exe = shutil.which('npm')
    if npm_exe:
        return npm_exe
    plat = get_platform()
    cmd = _pkg_install_cmd(plat, 'nodejs')
    if not cmd:
        return None
    print("\nNode.js and npm are required for Claude Code, Codex, and npm-based MCP servers.")
    ans = _prompt_choice(
        f"Install Node.js & npm via {' '.join(cmd[:3])}? (y/n)",
        ['y', 'yes', 'n', 'no'],
        default='y',
        non_interactive=non_interactive,
    )
    if ans in ('y', 'yes'):
        print(f"Running: {' '.join(cmd)}...")
        try:
            subprocess.run(cmd, check=False)
        except Exception as e:
            print(f"Failed to run package manager for Node.js: {e}")
    return shutil.which('npm')


def _ensure_litellm_proxy(venv_python: str) -> bool:
    """Installs `litellm[proxy]`, the Anthropic/Responses translation layer for engines lacking native endpoints."""
    res = subprocess.run([venv_python, '-c', 'import litellm.proxy.proxy_server'], capture_output=True)
    if res.returncode == 0:
        return True
    print("Installing LiteLLM proxy (litellm[proxy]) into llm-dash .venv...")
    return subprocess.run([venv_python, '-m', 'pip', 'install', '-q', '--no-warn-script-location', 'litellm[proxy]'], check=False).returncode == 0


def _ensure_uv_available(non_interactive: bool = False) -> Optional[str]:
    """Ensures `uvx` is available for Python-based MCP servers (`fetch`, `git`, `sqlite`, `pptx`, `repowise`)."""
    uvx_exe = shutil.which('uvx')
    if uvx_exe:
        return uvx_exe
    print("\nuvx (`uv`) is required for Python-based MCP servers.")
    ans = _prompt_choice(
        "Install `uv` via pip now? (y/n)",
        ['y', 'yes', 'n', 'no'],
        default='y',
        non_interactive=non_interactive,
    )
    if ans in ('y', 'yes'):
        try:
            subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'uv'], check=False)
        except Exception as e:
            print(f"Failed to install uv via pip: {e}")
    return shutil.which('uvx')


def setup_mcp_servers(non_interactive: bool = False) -> Dict[str, Any]:
    """
    Configures optional Model Context Protocol (MCP) servers across OpenCode, Claude Code, and Codex.
    Follows a 2-tier design:
      - Tier 1: Zero-config local servers (uvx / npx)
      - Tier 2: Cloud / API-key servers (GitHub, Brave Search, Tavily, Firecrawl)
    Strict opt-in: writes only selected servers; persists to config/config.json under 'mcp'.
    """
    from src.common.harness_config import (
        DEFAULT_MCP_CATALOG,
        write_claude_mcp_config,
        write_codex_profile,
    )
    from src.LLMServer.app import write_opencode_config
    from src.common.paths import load_config

    print("\n=== Model Context Protocol (MCP) Servers Setup (Optional) ===")
    print("MCP servers give your coding agents (OpenCode, Claude Code, Codex) tools for")
    print("web fetching, library docs search, document manipulation (docx/xlsx/pptx), browser automation, and reasoning.\n")

    tier1_keys = [k for k, v in DEFAULT_MCP_CATALOG.items() if v.get('tier') == 1]
    tier2_keys = [k for k, v in DEFAULT_MCP_CATALOG.items() if v.get('tier') == 2]

    selected_servers: Dict[str, Dict[str, Any]] = {}

    if non_interactive:
        cfg = load_config()
        mcp_data = cfg.get('mcp', {})
        enabled = mcp_data.get('enabled_servers', [])
        saved_servers = mcp_data.get('servers', {})
        if enabled:
            selected_servers = {k: v for k, v in saved_servers.items() if k in enabled}
        else:
            from src.common.harness_config import RECOMMENDED_MCP_SERVERS
            for k in RECOMMENDED_MCP_SERVERS:
                if k in DEFAULT_MCP_CATALOG:
                    spec = DEFAULT_MCP_CATALOG[k]
                    selected_servers[k] = {
                        'command': spec['command'],
                        'args': list(spec['args']),
                    }
    else:
        print("[Tier 1] Zero-Config Coding, Research & Document Servers (No API keys needed):")
        for i, k in enumerate(tier1_keys, 1):
            spec = DEFAULT_MCP_CATALOG[k]
            cmd = spec['command']
            desc = spec['description']
            print(f"  [{i}] {k:20s} - {desc} ({cmd})")
        print("\nTier 1 options:")
        print(f"  [S] Keep all disabled (Default / Recommended - toggle on as needed)")
        print(f"  [A] Enable ALL {len(tier1_keys)} Tier 1 servers")
        print("  [C] Custom select specific servers (comma-separated, e.g. 1,2,3,4)")

        ans = _prompt_choice("Select Tier 1 option (S/A/C)", ['s', 'skip', 'a', 'all', 'c', 'custom'], default='s', non_interactive=non_interactive).lower()

        chosen_tier1 = []
        if ans in ('a', 'all'):
            chosen_tier1 = tier1_keys
        elif ans in ('c', 'custom'):
            try:
                raw_pick = input("Enter numbers to enable (e.g. 1,2,4,5): ").strip()
                for part in raw_pick.replace(' ', ',').split(','):
                    if part.isdigit():
                        idx = int(part) - 1
                        if 0 <= idx < len(tier1_keys):
                            chosen_tier1.append(tier1_keys[idx])
            except (EOFError, KeyboardInterrupt):
                chosen_tier1 = []

        for k in chosen_tier1:
            spec = DEFAULT_MCP_CATALOG[k]
            selected_servers[k] = {
                'command': spec['command'],
                'args': list(spec['args']),
            }

        print("\n[Tier 2] Cloud & API-Key Coding & Research Servers (Optional):")
        for k in tier2_keys:
            spec = DEFAULT_MCP_CATALOG[k]
            desc = spec['description']
            env_var = spec.get('env_var', '')
            print(f"  - {k:15s} : {desc} (env: {env_var})")

        ans2 = _prompt_choice("Configure Tier 2 cloud servers? (y/n)", ['y', 'yes', 'n', 'no'], default='n', non_interactive=non_interactive).lower()
        if ans2 in ('y', 'yes'):
            for k in tier2_keys:
                spec = DEFAULT_MCP_CATALOG[k]
                env_var = spec.get('env_var', '')
                try:
                    val = input(f"Enter API key for {k} (or press Enter to set via ${env_var} later, or 'skip'): ").strip()
                except (EOFError, KeyboardInterrupt):
                    val = 'skip'
                if val.lower() != 'skip':
                    entry: Dict[str, Any] = {
                        'command': spec['command'],
                        'args': list(spec['args']),
                    }
                    if val:
                        entry['env'] = {env_var: val}
                    selected_servers[k] = entry

    cfg_file = REPO_ROOT / 'config' / 'config.json'
    if cfg_file.is_file():
        try:
            cfg_obj = json.loads(cfg_file.read_text(encoding='utf-8'))
        except Exception:
            cfg_obj = {}
        cfg_obj['mcp'] = {
            'enabled_servers': list(selected_servers.keys()),
            'servers': selected_servers,
        }
        cfg_file.write_text(json.dumps(cfg_obj, indent=2) + '\n', encoding='utf-8')

    alias = 'llamacpp'
    port = 8080
    context = 16384
    try:
        cfg = load_config()
        alias = cfg.get('settings', {}).get('last_alias') or alias
        port = int(cfg.get('settings', {}).get('server_port') or port)
        context = int(cfg.get('settings', {}).get('last_context') or context)
    except Exception:
        pass

    if any(spec.get('command') == 'uvx' for spec in selected_servers.values()):
        _ensure_uv_available(non_interactive=non_interactive)

    user_home = Path.home()
    base_url = f'http://127.0.0.1:{port}'

    write_opencode_config(REPO_ROOT, alias, port, context_limit=context, mcp_servers=selected_servers)
    write_codex_profile(user_home, base_url, alias, context=context, mcp_servers=selected_servers)
    write_claude_mcp_config(user_home, mcp_servers=selected_servers)

    enabled_list = list(selected_servers.keys())
    if enabled_list:
        print(f"Configured and enabled {len(enabled_list)} MCP servers across OpenCode, Claude Code, and Codex: {', '.join(enabled_list)}")
    else:
        print("MCP servers: strict opt-in retained (0 servers enabled across harnesses).")

    return selected_servers


def install_cli_agents(venv_python: str, non_interactive: bool = False):
    """Prompts user to install OpenCode, Claude Code and/or Codex CLI coding agents inside the llm-dash .venv."""
    venv_dir = REPO_ROOT / 'src' / 'LLMServer' / '.venv'
    _purge_aider(venv_python, venv_dir)
    print("\n=== CLI Coding Agents Setup (Installed in llm-dash .venv) ===")
    print("  [1] Install ALL: OpenCode, Claude Code & Codex in .venv (Recommended)")
    print("  [2] Install OpenCode only in .venv")
    print("  [3] Install Claude Code + Codex only in .venv")
    print("  [4] Skip CLI agent installation")

    choice = _prompt_choice("Select CLI agent option (1/2/3/4)", ['1', '2', '3', '4'], default='1', non_interactive=non_interactive)

    if choice in ('1', '2'):
        print("\nInstalling OpenCode CLI into llm-dash .venv...")
        _install_opencode_into_venv(venv_dir, non_interactive=non_interactive)
        setup_opencode_orchestrator()

    if choice in ('1', '3'):
        _ensure_npm_available(non_interactive)
        _ensure_litellm_proxy(venv_python)
        for name in NPM_HARNESS_PACKAGES:
            _install_npm_harness(venv_dir, name, non_interactive=non_interactive)


def setup_opencode_orchestrator():
    """Ensures antigravity-skill-orchestrator skill is present in ~/.config/opencode/skills."""
    src_skill = REPO_ROOT / 'src' / 'common' / 'skills' / 'antigravity-skill-orchestrator' / 'SKILL.md'
    if not src_skill.is_file():
        return
    dest_dir = Path.home() / '.config' / 'opencode' / 'skills' / 'antigravity-skill-orchestrator'
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_skill = dest_dir / 'SKILL.md'
    try:
        shutil.copy2(src_skill, dest_skill)
    except Exception:
        pass





def setup_agent_configs(home: Optional[Path] = None, port: int = 8080, non_interactive: bool = False) -> Dict[str, Path]:
    """
    Configures Claude Code (~/.claude/settings.json) and Codex (~/.codex/config.toml & llmcode.config.toml)
    with model=llamacpp, bypassPermissions, and llmdash context size.
    """
    from src.common.harness_config import setup_all_agent_configs
    print("\n=== Coding Agent Configurations (Claude Code & Codex) ===")
    try:
        written = setup_all_agent_configs(home=home, port=port, non_interactive=non_interactive)
        print(f"  ✓ Claude Code settings : {written.get('claude')} (model=llamacpp, permissions=bypassPermissions)")
        print(f"  ✓ Codex profile        : {written.get('codex_profile')} (model=llamacpp, approval_policy=never)")
        print(f"  ✓ Codex default config : {written.get('codex_config')}")
        return written
    except Exception as e:
        print(f"  Warning: failed to configure agent profiles: {e}")
        return {}


def render_desktop_entry(repo_root: Path = REPO_ROOT) -> Optional[Path]:
    """Renders bin/llmCode.desktop from bin/llmCode.desktop.in with absolute paths."""
    in_file = repo_root / 'bin' / 'llmCode.desktop.in'
    out_file = repo_root / 'bin' / 'llmCode.desktop'
    if not in_file.is_file():
        return None
    try:
        content = in_file.read_text(encoding='utf-8')
        rendered = content.replace('@LLMCODE_DIR@', str(repo_root.resolve()))
        out_file.write_text(rendered, encoding='utf-8')
        if os.name != 'nt':
            try:
                out_file.chmod(0o755)
            except Exception:
                pass
        return out_file
    except Exception as e:
        print(f"Warning: Failed to render desktop entry: {e}")
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
    - Deletes downloaded binaries in bin/ (preserving bin/llmCode.desktop.in and bin/llmCode.svg)
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
            if item.name not in ('llmCode.desktop', 'llmCode.desktop.in', 'llmCode.svg'):
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

    # 5. Install CLI coding agents (OpenCode, Claude Code & Codex) inside llm-dash .venv
    venv_py = REPO_ROOT / 'src' / 'LLMServer' / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python3')
    py_exe = str(venv_py) if venv_py.is_file() else sys.executable
    install_cli_agents(py_exe, non_interactive=non_interactive)

    # 5.5. Configure Model Context Protocol (MCP) servers across OpenCode, Claude Code, and Codex
    setup_mcp_servers(non_interactive=non_interactive)

    # 6.5. Deploy OpenCode orchestrator skill
    setup_opencode_orchestrator()

    # 6.6. Configure Claude Code and Codex profiles with llamacpp model, bypassPermissions, and llmdash context
    setup_agent_configs(non_interactive=non_interactive)

    # 7. Render desktop entry template with absolute paths
    render_desktop_entry()

    # 8. Sync all resolved paths & hardware-tuned defaults into config/config.json
    cfg = sync_config(apply_hardware_defaults=True)
    print("\n=== Configuration Database Updated (config/config.json & config/hardware.json) ===")
    print(f"  Hardware Profile : {HARDWARE_FILE}")
    print(f"  llama.cpp        : {cfg['binaries']['llama_cpp']} (ready={cfg['binaries']['llama_cpp_exists']})")
    print(f"  koboldcpp        : {cfg['binaries']['koboldcpp']} (ready={cfg['binaries']['koboldcpp_exists']})")
    print(f"  ollama           : {cfg['binaries']['ollama']} (ready={cfg['binaries']['ollama_exists']})")
    for name in ('opencode', 'claude', 'codex'):
        print(f"  {name + ' (.venv)':<17}: {cfg['agents'].get(name) or 'not installed'}")


if __name__ == '__main__':
    non_int = '--non-interactive' in sys.argv or '-y' in sys.argv
    uninst = '--uninstall' in sys.argv
    main(non_interactive=non_int, uninstall=uninst)
