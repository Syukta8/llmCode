#!/usr/bin/env bash
set -euo pipefail

BASE="${LOCAL_AI_HOME:-$HOME/llmCode}"
SRC="$BASE/src/llama.cpp"

# Resolve NVCC path dynamically
NVCC="$(command -v nvcc 2>/dev/null || true)"
if [[ -z "$NVCC" || ! -x "$NVCC" ]]; then
  for candidate in /usr/local/cuda-12.8/bin/nvcc /usr/local/cuda/bin/nvcc; do
    if [[ -x "$candidate" ]]; then
      NVCC="$candidate"
      break
    fi
  done
fi

[[ -n "$NVCC" && -x "$NVCC" ]] || { echo "CUDA compiler (nvcc) not found" >&2; exit 1; }

mkdir -p "$BASE/src" "$BASE/models" "$BASE/logs" "$BASE/bin"

# Git repository checkout and optional update
if [[ ! -d "$SRC/.git" ]]; then
  echo "Cloning llama.cpp into $SRC..."
  git clone https://github.com/ggml-org/llama.cpp.git "$SRC"
else
  echo "Existing checkout found: $SRC"
  read -r -p "Update llama.cpp to latest git master? [y/N]: " ans || ans="n"
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    echo "Updating llama.cpp..."
    git -C "$SRC" pull --ff-only || { echo "Git pull failed; continuing with current tree" >&2; }
  fi
fi

cd "$SRC"
git rev-parse HEAD | tee "$BASE/llama-commit.txt"

# CMake configuration:
# - CMAKE_INTERPROCEDURAL_OPTIMIZATION=ON enables Link-Time Optimization (LTO)
# - GGML_NATIVE=ON targets host CPU instruction sets (AVX2, FMA, BMI2) for offloaded layers
# - GGML_CCACHE=ON accelerates rebuilds with compiler cache
# - GGML_CUDA=ON enables NVIDIA GPU backend
# - CMAKE_CUDA_ARCHITECTURES=61 targets GTX 1050 (Pascal)
# - GGML_CUDA_FORCE_MMQ=ON boosts inference for quantized models on Pascal (no Tensor Cores)
# - GGML_CUDA_NCCL=OFF disables multi-GPU clustering checks (single laptop GPU)
# - GGML_CUDA_FA_ALL_QUANTS=ON compiles Flash Attention kernels for all quantized formats
# Clean build directory to prevent CMakeCache reset wipeout
rm -rf build-cuda

cmake -S . -B build-cuda -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=61 \
  -DCMAKE_CUDA_COMPILER="$NVCC" \
  -DCMAKE_C_COMPILER=/usr/bin/gcc-13 \
  -DCMAKE_CXX_COMPILER=/usr/bin/g++-13 \
  -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-13 \
  -DGGML_NATIVE=ON \
  -DGGML_CCACHE=ON \
  -DGGML_CUDA_FORCE_MMQ=ON \
  -DGGML_CUDA_NCCL=OFF \
  -DGGML_CUDA_FA_ALL_QUANTS=ON \
  -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=ON

# Compile with 4 parallel jobs to avoid memory exhaustion during LTO link stage
cmake --build build-cuda --target llama-server llama-cli llama-bench -j 4

"$SRC/build-cuda/bin/llama-server" --version
