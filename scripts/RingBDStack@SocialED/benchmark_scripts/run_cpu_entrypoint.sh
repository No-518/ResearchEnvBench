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

exec "$HOST_PYTHON" "$ROOT_DIR/benchmark_scripts/runner.py" run \
  --stage cpu \
  --task infer \
  --framework pytorch \
  --timeout-sec 600 \
  --decision-reason "Repo is a library (no CLI entrypoint detected); use public API via SocialED.detector.SBERT on prepared Event2012 subset and local SBERT model path; force CPU by CUDA_VISIBLE_DEVICES=''." \
  --env "PYTHONDONTWRITEBYTECODE=1" \
  --env "CUDA_VISIBLE_DEVICES=" \
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
  @python -u "$ROOT_DIR/benchmark_scripts/socialed_sbert_smoketest.py" \
    --prepare-results "$ROOT_DIR/build_output/prepare/results.json" \
    --max-samples 12 \
    --artifact-dir "$ROOT_DIR/build_output/cpu"
