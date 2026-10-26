#!/usr/bin/env bash
set -euo pipefail
if [[ $(id -u) -eq 0 ]]; then echo 'Run as normal user with sudo, not root.' >&2; exit 1; fi
command -v nvidia-smi >/dev/null || { echo 'NVIDIA driver missing; stop.' >&2; exit 1; }
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
sudo apt-get update
sudo apt-get install -y curl ca-certificates git cmake ninja-build build-essential ccache libcurl4-openssl-dev jq python3 python3-venv gcc-13 g++-13
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
curl -fL --retry 3 -o "$TMP/cuda-keyring.deb" 'https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb'
sudo dpkg -i "$TMP/cuda-keyring.deb"
sudo apt-get update
printf '\nInspect the proposed CUDA toolkit transaction. It should not replace your NVIDIA driver.\n'
sudo apt-get -s install cuda-toolkit-12-8
read -r -p 'Proceed with toolkit-only installation? [y/N] ' answer
[[ "$answer" =~ ^[Yy]$ ]] || exit 0
sudo apt-get install -y cuda-toolkit-12-8
/usr/local/cuda-12.8/bin/nvcc --version
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
