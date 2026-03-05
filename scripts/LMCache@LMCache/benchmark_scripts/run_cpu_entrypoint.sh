#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

manifest_path="benchmark_assets/manifest.json"

model_path=""
dataset_path=""
config_path=""

if [[ -f "$manifest_path" ]]; then
  model_path="$(python3 -c 'import json; print(json.load(open("benchmark_assets/manifest.json","r",encoding="utf-8")).get("model",{}).get("path",""))' || true)"
  dataset_path="$(python3 -c 'import json; print(json.load(open("benchmark_assets/manifest.json","r",encoding="utf-8")).get("dataset",{}).get("path",""))' || true)"
  config_path="$(python3 -c 'import json; print(json.load(open("benchmark_assets/manifest.json","r",encoding="utf-8")).get("lmcache_config",{}).get("path",""))' || true)"
fi

export CUDA_VISIBLE_DEVICES=""
export SCIMLOPSBENCH_DATASET_PATH="$dataset_path"
export SCIMLOPSBENCH_MODEL_PATH="$model_path"

timeout_sec="${SCIMLOPSBENCH_CPU_TIMEOUT_SEC:-600}"
run_duration_sec="${SCIMLOPSBENCH_ENTRYPOINT_RUN_DURATION_SEC:-8}"

kv_shape="${SCIMLOPSBENCH_KV_SHAPE:-2,2,8,2,8}"
kvcache_spec="${SCIMLOPSBENCH_KVCACHE_SHAPE_SPEC:-(2,2,8,2,8):float16:2}"

decision_reason="Run LMCache standalone starter briefly (timeout ${run_duration_sec}s) to validate CPU initialization using minimal KV shapes; dataset/model paths come from benchmark_assets/manifest.json."

python3 benchmark_scripts/runner.py run \
  --stage cpu \
  --task infer \
  --framework pytorch \
  --timeout-sec "$timeout_sec" \
  --decision-reason "$decision_reason" \
  -- \
  timeout --preserve-status --signal=SIGINT "${run_duration_sec}s" \
    "{python}" -m lmcache.v1.standalone \
      --config "$config_path" \
      --model-name "$model_path" \
      --worker-id 0 \
      --world-size 1 \
      --kv-shape "$kv_shape" \
      --kvcache-shape-spec "$kvcache_spec" \
      --kv-dtype float16 \
      --device cpu

