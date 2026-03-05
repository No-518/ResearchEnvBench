#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_PYTHON="$(command -v python3 || command -v python || true)"
if [[ -z "$HOST_PYTHON" ]]; then
  echo "python3/python missing in PATH" >&2
  exit 1
fi

export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

CUDA_VISIBLE="${CUDA_VISIBLE_DEVICES:-0,1}"
NPROC="${SCIMLOPSBENCH_NPROC_PER_NODE:-2}"

# If CUDA stage produced a GPU count, enforce >=2 before attempting multi-GPU.
CUDA_RESULTS="$ROOT_DIR/build_output/cuda/results.json"
GPU_COUNT=""
if [[ -f "$CUDA_RESULTS" ]]; then
  GPU_COUNT="$("$HOST_PYTHON" - <<PY
import json
from pathlib import Path
p = Path("$CUDA_RESULTS")
try:
  d = json.loads(p.read_text(encoding="utf-8"))
  obs = d.get("observed", {})
  print(obs.get("gpu_count", ""))
except Exception:
  print("")
PY
)"
fi

if [[ -n "${GPU_COUNT:-}" ]]; then
  if [[ "${GPU_COUNT}" =~ ^[0-9]+$ ]] && [[ "${GPU_COUNT}" -lt 2 ]]; then
    exec "$HOST_PYTHON" "$ROOT_DIR/benchmark_scripts/runner.py" run \
      --stage multi_gpu \
      --task infer \
      --framework pytorch \
      --timeout-sec 1200 \
      --decision-reason "Insufficient hardware: need >=2 GPUs for multi-GPU stage; observed gpu_count=${GPU_COUNT} (from build_output/cuda/results.json)." \
      --no-python-needed \
      -- \
      bash -lc "echo 'Insufficient hardware: need >=2 GPUs; observed ${GPU_COUNT}'; exit 1"
  fi
fi

exec "$HOST_PYTHON" "$ROOT_DIR/benchmark_scripts/runner.py" run \
  --stage multi_gpu \
  --task infer \
  --framework pytorch \
  --timeout-sec 1200 \
  --decision-reason "Best-effort multi-GPU smoke test via torch.distributed.run + SocialED.detector.SBERT; force CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE} (default 0,1) and nproc_per_node=${NPROC}." \
  --env "PYTHONDONTWRITEBYTECODE=1" \
  --env "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE}" \
  --env "HF_HOME=$ROOT_DIR/benchmark_assets/cache/huggingface" \
  --env "HF_HUB_CACHE=$ROOT_DIR/benchmark_assets/cache/huggingface/hub" \
  --env "TRANSFORMERS_CACHE=$ROOT_DIR/benchmark_assets/cache/huggingface/transformers" \
  --env "SENTENCE_TRANSFORMERS_HOME=$ROOT_DIR/benchmark_assets/cache/sentence_transformers" \
  --env "TORCH_HOME=$ROOT_DIR/benchmark_assets/cache/torch" \
  --env "XDG_CACHE_HOME=$ROOT_DIR/benchmark_assets/cache/xdg" \
  --env "HF_HUB_OFFLINE=1" \
  --env "TRANSFORMERS_OFFLINE=1" \
  --env "TOKENIZERS_PARALLELISM=false" \
  -- \
  @python -u -m torch.distributed.run \
    --nproc_per_node "${NPROC}" \
    "$ROOT_DIR/benchmark_scripts/socialed_sbert_smoketest.py" \
      --prepare-results "$ROOT_DIR/build_output/prepare/results.json" \
      --max-samples 12 \
      --artifact-dir "$ROOT_DIR/build_output/multi_gpu" \
      --distributed \
      --require-gpu-count 2
