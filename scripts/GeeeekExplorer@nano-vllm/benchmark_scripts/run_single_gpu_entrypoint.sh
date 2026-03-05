#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/build_output/single_gpu"
ASSETS_JSON="$REPO_ROOT/benchmark_assets/assets.json"
CACHE_DIR="$REPO_ROOT/benchmark_assets/cache"

mkdir -p "$OUT_DIR"

PYTHON_BOOTSTRAP=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BOOTSTRAP="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BOOTSTRAP="python"
else
  echo "ERROR: python/python3 not found in PATH" >&2
  exit 1
fi

PYCODE="$(cat <<'PY'
import json
import os

from nanovllm import LLM, SamplingParams

assets = json.load(open("benchmark_assets/assets.json", "r", encoding="utf-8"))
model_path = assets["model"]["path"]
dataset_path = assets["dataset"]["path"]
dataset = json.load(open(dataset_path, "r", encoding="utf-8"))
prompt = dataset["prompts"][0]

llm = LLM(
    model_path,
    enforce_eager=True,
    tensor_parallel_size=1,
    max_model_len=256,
    max_num_batched_tokens=512,
    max_num_seqs=1,
)
sp = SamplingParams(temperature=0.6, max_tokens=1)
out = llm.generate([prompt], sp, use_tqdm=False)
print(out[0]["text"])
PY
)"

"$PYTHON_BOOTSTRAP" "$REPO_ROOT/benchmark_scripts/runner.py" \
  --stage single_gpu \
  --task infer \
  --framework pytorch \
  --timeout-sec 600 \
  --assets-json "$ASSETS_JSON" \
  --decision-reason "Run Nano-vLLM inference via the documented LLM/SamplingParams API (README Quick Start), minimized to 1 prompt and max_tokens=1." \
  --env CUDA_VISIBLE_DEVICES=0 \
  --env HF_HOME="$CACHE_DIR/huggingface" \
  --env HF_HUB_CACHE="$CACHE_DIR/huggingface/hub" \
  --env TRANSFORMERS_CACHE="$CACHE_DIR/transformers" \
  --env XDG_CACHE_HOME="$CACHE_DIR/xdg" \
  --env TORCH_HOME="$CACHE_DIR/torch" \
  --env TRITON_CACHE_DIR="$CACHE_DIR/triton" \
  -- python -c "$PYCODE"
