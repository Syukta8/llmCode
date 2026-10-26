# -*- coding: utf-8 -*-
"""
Cross-platform binary setup & downloader for llama.cpp, koboldcpp, and ollama.
Supports both Windows (10/11) and Linux (Ubuntu/Debian).
"""
import os
import sys
import shutil
import zipfile
import tarfile
import urllib.request
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.paths import get_llama_server_binary, get_store_dir, get_executable_name

def download_file(url: str, dest: Path) -> Path:
    """Download a file with progress tracking."""
    print(f"Downloading {url} ...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={'User-Agent': 'llmCode-Installer/1.0'})
    with urllib.request.urlopen(req) as resp, open(dest, 'wb') as f:
        shutil.copyfileobj(resp, f)
    print(f"Saved to {dest}")
    return dest

def setup_windows_llama_cpp(cuda_ver: str = "cu12.4"):
    """
    Downloads latest official pre-built Windows llama.cpp CUDA binaries from GitHub.
    Places them inside bin/windows/.
    """
    dest_dir = REPO_ROOT / 'bin' / 'windows'
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    print("Querying latest llama.cpp release from GitHub API...")
    api_url = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    req = urllib.request.Request(api_url, headers={'User-Agent': 'llmCode-Installer/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            release_data = json.loads(r.read())
    except Exception as e:
        print(f"Failed to query GitHub release: {e}")
        return False

    # Look for matching asset: e.g. llama-bXXXX-bin-win-cuda-cu12.4-x64.zip
    target_asset = None
    for asset in release_data.get('assets', []):
        name = asset.get('name', '').lower()
        if 'bin-win' in name and 'x64' in name and ('cuda' in name or 'cu12' in name):
            target_asset = asset
            break

    if not target_asset:
        # Fallback to AVX2/AVX build if cuda asset pattern differs
        for asset in release_data.get('assets', []):
            name = asset.get('name', '').lower()
            if 'bin-win' in name and 'x64' in name:
                target_asset = asset
                break

    if not target_asset:
        print("No suitable pre-built Windows binary asset found in latest release.")
        return False

    zip_path = dest_dir / target_asset['name']
    download_file(target_asset['browser_download_url'], zip_path)
    
    print(f"Extracting {zip_path.name} to {dest_dir} ...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(dest_dir)
    
    zip_path.unlink(missing_ok=True)
    print("llama.cpp Windows binary installed successfully into bin/windows/")
    return True

def compile_linux_llama_cpp():
    """Runs CMake compilation on Linux targeting CUDA."""
    cmake_bin = shutil.which('cmake')
    if not cmake_bin:
        raise RuntimeError("cmake not found in PATH")
    
    src_dir = REPO_ROOT / 'src' / 'llama.cpp'
    build_dir = src_dir / 'build-cuda'
    
    if not (src_dir / '.git').is_dir():
        print(f"Cloning llama.cpp into {src_dir} ...")
        subprocess.run(['git', 'clone', 'https://github.com/ggml-org/llama.cpp.git', str(src_dir)], check=True)
        
    print("Configuring CMake for Linux CUDA...")
    subprocess.run([
        'cmake', '-S', str(src_dir), '-B', str(build_dir), '-G', 'Ninja',
        '-DCMAKE_BUILD_TYPE=Release',
        '-DGGML_CUDA=ON',
        '-DGGML_NATIVE=ON',
        '-DGGML_CUDA_FORCE_MMQ=ON',
        '-DGGML_CUDA_FA_ALL_QUANTS=ON'
    ], check=True)
    
    print("Building llama-server ...")
    subprocess.run(['cmake', '--build', str(build_dir), '--target', 'llama-server', '-j', '4'], check=True)
    print("Linux llama-server compilation complete.")

def main():
    if os.name == 'nt':
        print("Windows detected.")
        setup_windows_llama_cpp()
    else:
        print("Linux detected.")
        compile_linux_llama_cpp()

if __name__ == '__main__':
    main()
