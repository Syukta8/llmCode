# -*- coding: utf-8 -*-
"""
Interactive cross-platform installer, prerequisite verifier, and source compiler
for llmCode backend engines (llama.cpp, koboldcpp, ollama) and CLI coding agents
(aider, opencode) across Windows, Linux, and macOS.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.paths import (
    BIN_DIR,
    SRC_DIR,
    get_executable_name,
    get_koboldcpp_binary,
    get_llama_server_binary,
    get_ollama_binary,
    get_platform_name,
    sync_config,
)


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
            return True, shutil.which('g++') or shutil.which('clang++')
        return False, "Missing MSVC Build Tools (cl.exe) or MinGW g++"
    else:
        for comp in ('g++', 'clang++', 'c++'):
            found = shutil.which(comp)
            if found:
                return True, found
        return False, "Missing C++ compiler (g++ / clang++)"


def inspect_requirements(needs_llama_src: bool, needs_kobold_src: bool, needs_ollama_src: bool, accel: str) -> List[Dict]:
    """
    Inspects system for required build tools based on selected engines to compile.
    Returns a list of requirement dicts: {name, found, detail, install_cmd, manual_guide}.
    """
    plat = get_platform_name()
    reqs = []

    if not (needs_llama_src or needs_kobold_src or needs_ollama_src):
        return reqs

    # 1. Git
    git_bin = shutil.which('git')
    reqs.append({
        'name': 'git',
        'found': bool(git_bin),
        'detail': git_bin or 'Not found in PATH',
        'install_cmd': _pkg_install_cmd(plat, 'git'),
        'manual_guide': _manual_guide(plat, 'git'),
    })

    # 2. C/C++ Compiler
    has_cc, cc_detail = check_c_compiler()
    reqs.append({
        'name': 'c++ compiler',
        'found': has_cc,
        'detail': cc_detail,
        'install_cmd': _pkg_install_cmd(plat, 'compiler'),
        'manual_guide': _manual_guide(plat, 'compiler'),
    })

    # 3. CMake + Ninja (needed by llama.cpp and ollama source builds)
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

    # 4. Make (needed by koboldcpp on Linux/macOS)
    if needs_kobold_src and plat != 'windows':
        make_bin = shutil.which('make')
        reqs.append({
            'name': 'make',
            'found': bool(make_bin),
            'detail': make_bin or 'Not found in PATH',
            'install_cmd': _pkg_install_cmd(plat, 'make'),
            'manual_guide': _manual_guide(plat, 'make'),
        })

    # 5. Go 1.22+ (needed by ollama source build)
    if needs_ollama_src:
        go_bin = shutil.which('go')
        reqs.append({
            'name': 'golang (go)',
            'found': bool(go_bin),
            'detail': go_bin or 'Not found in PATH',
            'install_cmd': _pkg_install_cmd(plat, 'go'),
            'manual_guide': _manual_guide(plat, 'go'),
        })

    # 6. CUDA Toolkit (nvcc) if CUDA GPU detected
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
        if tool == 'compiler' or tool == 'make':
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


def verify_and_install_requirements(reqs: List[Dict], non_interactive: bool = False) -> bool:
    """
    Displays requirement check results. If any required tool is missing, asks user
    permission to auto-install. If declined, prints manual installation guidance.
    Returns True if all mandatory requirements are satisfied.
    """
    if not reqs:
        return True

    print("\n--- Build Requirements Check ---")
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
        # Re-verify mandatory requirements
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
# Engine 1: llama.cpp
# ==============================================================================
def compile_llama_cpp(accel: str) -> bool:
    """Clones and compiles llama.cpp inside src/llama.cpp across Windows, Linux, and macOS."""
    src_dir = SRC_DIR / 'llama.cpp'
    if not (src_dir / 'CMakeLists.txt').is_file():
        print(f"Cloning llama.cpp into {src_dir} ...")
        subprocess.run(['git', 'clone', '--depth', '1', 'https://github.com/ggml-org/llama.cpp.git', str(src_dir)], check=True)

    # Check if nvcc actually exists before enabling CUDA
    use_cuda = accel == 'cuda' and bool(shutil.which('nvcc') or Path('/usr/local/cuda/bin/nvcc').is_file())
    use_metal = accel == 'metal'

    build_subdir = 'build-cuda' if use_cuda else ('build-metal' if use_metal else 'build')
    build_dir = src_dir / build_subdir

    cmake_args = [
        'cmake', '-S', str(src_dir), '-B', str(build_dir),
        '-DCMAKE_BUILD_TYPE=Release',
    ]
    if shutil.which('ninja'):
        cmake_args.extend(['-G', 'Ninja'])

    if use_cuda:
        cmake_args.extend([
            '-DGGML_CUDA=ON',
            '-DGGML_CUDA_FORCE_MMQ=ON',
            '-DGGML_CUDA_FA_ALL_QUANTS=ON',
        ])
    elif use_metal:
        cmake_args.append('-DGGML_METAL=ON')

    jobs = str(max(2, (os.cpu_count() or 4) // 2))
    build_cmd = ['cmake', '--build', str(build_dir), '--config', 'Release', '--target', 'llama-server', '-j', jobs]

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

    # Locate extracted llama-server and symlink/copy to bin/
    exe_name = get_executable_name('llama-server')
    for p in dest_dir.rglob(exe_name):
        if p.is_file():
            if os.name != 'nt':
                p.chmod(0o755)
            top_bin = BIN_DIR / exe_name
            if not top_bin.exists():
                try:
                    shutil.copy2(p, top_bin)
                except Exception:
                    pass
            break

    print(f"Prebuilt llama.cpp installed in {dest_dir}")
    return True


# ==============================================================================
# Engine 2: KoboldCpp
# ==============================================================================
def compile_koboldcpp(accel: str) -> bool:
    """Clones and compiles KoboldCpp inside src/koboldcpp."""
    src_dir = SRC_DIR / 'koboldcpp'
    if not (src_dir / 'koboldcpp.py').is_file():
        print(f"Cloning KoboldCpp into {src_dir} ...")
        subprocess.run(['git', 'clone', '--depth', '1', 'https://github.com/LostRuins/koboldcpp.git', str(src_dir)], check=True)

    plat = get_platform_name()
    if plat == 'windows':
        # On Windows, KoboldCpp provides make_pyinstaller_cuda.bat / CMakeLists.txt
        cmake_Args = ['cmake', '-S', str(src_dir), '-B', str(src_dir / 'build'), '-DCMAKE_BUILD_TYPE=Release']
        try:
            subprocess.run(cmake_Args, check=True)
            subprocess.run(['cmake', '--build', str(src_dir / 'build'), '--config', 'Release'], check=True)
            return True
        except Exception as e:
            print(f"Windows KoboldCpp local build note ({e}); downloading official prebuilt binary fallback...")
            return download_prebuilt_koboldcpp(accel)
    else:
        use_cuda = accel == 'cuda' and bool(shutil.which('nvcc') or Path('/usr/local/cuda/bin/nvcc').is_file())
        jobs = str(max(2, (os.cpu_count() or 4) // 2))
        make_cmd = ['make', '-C', str(src_dir), f'-j{jobs}', 'LLAMA_PORTABLE=1']
        if use_cuda:
            make_cmd.append('LLAMA_CUBLAS=1')
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
# Engine 3: Ollama
# ==============================================================================
def compile_ollama(accel: str) -> bool:
    """Clones and compiles Ollama from source inside src/ollama."""
    src_dir = SRC_DIR / 'ollama'
    if not (src_dir / 'go.mod').is_file():
        print(f"Cloning Ollama into {src_dir} ...")
        subprocess.run(['git', 'clone', '--depth', '1', 'https://github.com/ollama/ollama.git', str(src_dir)], check=True)

    exe_name = get_executable_name('ollama')
    try:
        if (src_dir / 'CMakeLists.txt').is_file() and shutil.which('cmake'):
            subprocess.run(['cmake', '-S', str(src_dir), '-B', str(src_dir / 'build')], check=True)
            subprocess.run(['cmake', '--build', str(src_dir / 'build'), '--parallel'], check=True)
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
# CLI Agents: Aider & OpenCode
# ==============================================================================
def install_cli_agents(venv_python: str, non_interactive: bool = False):
    """Prompts user to install Aider and/or OpenCode CLI coding agents."""
    print("\n=== CLI Coding Agents Setup (Aider & OpenCode) ===")
    print("  [1] Install BOTH Aider & OpenCode (Recommended)")
    print("  [2] Install Aider only")
    print("  [3] Install OpenCode only")
    print("  [4] Skip CLI agent installation")

    choice = _prompt_choice("Select CLI agent option (1/2/3/4)", ['1', '2', '3', '4'], default='1', non_interactive=non_interactive)

    if choice in ('1', '2'):
        print("\nInstalling Aider CLI (aider-chat)...")
        try:
            subprocess.run([venv_python, '-m', 'pip', 'install', '-q', 'aider-chat'], check=True)
            print("Aider installed successfully in virtualenv.")
        except Exception as e:
            print(f"Warning: Aider pip install encountered an issue: {e}")
            print("Manual guidance: Run `pip install aider-chat` or `uv tool install aider-chat`")

    if choice in ('1', '3'):
        print("\nChecking OpenCode CLI...")
        opencode_bin = Path.home() / '.opencode' / 'bin' / get_executable_name('opencode')
        if opencode_bin.is_file() or shutil.which('opencode'):
            print("OpenCode CLI is already installed.")
        else:
            if shutil.which('npm'):
                print("Installing OpenCode via npm (`npm install -g opencode-ai`)...")
                try:
                    subprocess.run(['npm', 'install', '-g', 'opencode-ai'], check=True)
                    print("OpenCode installed successfully via npm.")
                except Exception as e:
                    print(f"Warning: npm install failed ({e}).")
            elif os.name != 'nt' and shutil.which('curl'):
                print("Installing OpenCode via official installer script...")
                try:
                    subprocess.run('curl -fsSL https://opencode.ai/install | bash', shell=True, check=True)
                except Exception as e:
                    print(f"Warning: OpenCode curl installer failed ({e}).")
                    print("Manual guidance: Install Node.js and run `npm install -g opencode-ai` or visit https://opencode.ai")
            else:
                print("Manual guidance for OpenCode: Install Node.js (https://nodejs.org) and run `npm install -g opencode-ai`.")


def main(non_interactive: bool = False):
    plat = get_platform_name()
    accel = detect_gpu_acceleration()
    print(f"\n=== llmCode Backend & Agent Installer ({plat.upper()} | Acceleration: {accel.upper()}) ===")

    engines = [
        ('llama.cpp', get_llama_server_binary()),
        ('koboldcpp', get_koboldcpp_binary()),
        ('ollama', get_ollama_binary()),
    ]

    selections = {}
    for eng_name, existing_bin in engines:
        exists_note = f" [Already present at {existing_bin}]" if existing_bin.is_file() else " [Not installed]"
        print(f"\nBackend Engine: {eng_name}{exists_note}")
        print(f"  [1] Compile from source in src/{eng_name}")
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

    # Check build requirements if any engine is set to compile from source
    reqs = inspect_requirements(needs_llama_src, needs_kobold_src, needs_ollama_src, accel)
    can_compile = verify_and_install_requirements(reqs, non_interactive=non_interactive)

    # Execute engine actions
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

    # Install CLI coding agents
    venv_py = REPO_ROOT / 'src' / 'LLMServer' / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python3')
    py_exe = str(venv_py) if venv_py.is_file() else sys.executable
    install_cli_agents(py_exe, non_interactive=non_interactive)

    # Sync all resolved paths into config/config.json
    cfg = sync_config()
    print("\n=== Configuration Database Updated (config/config.json) ===")
    print(f"  llama.cpp : {cfg['binaries']['llama_cpp']} (ready={cfg['binaries']['llama_cpp_exists']})")
    print(f"  koboldcpp : {cfg['binaries']['koboldcpp']} (ready={cfg['binaries']['koboldcpp_exists']})")
    print(f"  ollama    : {cfg['binaries']['ollama']} (ready={cfg['binaries']['ollama_exists']})")
    print(f"  aider     : {cfg['agents']['aider'] or 'not installed'}")
    print(f"  opencode  : {cfg['agents']['opencode'] or 'not installed'}")


if __name__ == '__main__':
    non_int = '--non-interactive' in sys.argv or '-y' in sys.argv
    main(non_interactive=non_int)
