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

devices="${SCIMLOPSBENCH_MULTI_GPU_DEVICES:-0,1}"
nproc="${SCIMLOPSBENCH_MULTI_GPU_NPROC:-2}"

export CUDA_VISIBLE_DEVICES="$devices"
export SCIMLOPSBENCH_DATASET_PATH="$dataset_path"
export SCIMLOPSBENCH_MODEL_PATH="$model_path"
export SCIMLOPSBENCH_LMCACHE_CONFIG_PATH="$config_path"
export SCIMLOPSBENCH_MULTI_GPU_NPROC="$nproc"

timeout_sec="${SCIMLOPSBENCH_MULTI_GPU_TIMEOUT_SEC:-1200}"
run_duration_sec="${SCIMLOPSBENCH_ENTRYPOINT_RUN_DURATION_SEC:-8}"

kv_shape="${SCIMLOPSBENCH_KV_SHAPE:-2,2,8,2,8}"
kvcache_spec="${SCIMLOPSBENCH_KVCACHE_SHAPE_SPEC:-(2,2,8,2,8):float16:2}"

decision_reason="Launch LMCache standalone starter under torch.distributed.run with ${nproc} local ranks and per-rank device cuda:\${LOCAL_RANK}; fail if fewer than ${nproc} visible GPUs."

python3 benchmark_scripts/runner.py run \
  --stage multi_gpu \
  --task infer \
  --framework pytorch \
  --timeout-sec "$timeout_sec" \
  --decision-reason "$decision_reason" \
  -- \
  bash -lc '
    set -euo pipefail
    TARGET_PYTHON="$1"
    NPROC="${SCIMLOPSBENCH_MULTI_GPU_NPROC:-2}"
    RUN_DUR="${SCIMLOPSBENCH_ENTRYPOINT_RUN_DURATION_SEC:-8}"
    KV_SHAPE="${SCIMLOPSBENCH_KV_SHAPE:-2,2,8,2,8}"
    KVCACHE_SPEC="${SCIMLOPSBENCH_KVCACHE_SHAPE_SPEC:-(2,2,8,2,8):float16:2}"
    MODEL_PATH="${SCIMLOPSBENCH_MODEL_PATH:-}"
    CONFIG_PATH="${SCIMLOPSBENCH_LMCACHE_CONFIG_PATH:-}"

    echo "[multi_gpu] TARGET_PYTHON=$TARGET_PYTHON"
    echo "[multi_gpu] CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-}"
    echo "[multi_gpu] NPROC=$NPROC"

    gpu_count="$("$TARGET_PYTHON" -c "import torch; print(torch.cuda.device_count() if torch.cuda.is_available() else 0)")"
    echo "[multi_gpu] torch.cuda.device_count()=$gpu_count"
    if [[ "$gpu_count" -lt "$NPROC" ]]; then
      echo "[multi_gpu] ERROR: need >=${NPROC} visible GPUs, found ${gpu_count}"
      exit 1
    fi

    inner_cmd="timeout --preserve-status --signal=SIGINT ${RUN_DUR}s \"${TARGET_PYTHON}\" -m lmcache.v1.standalone --config \"${CONFIG_PATH}\" --model-name \"${MODEL_PATH}\" --worker-id \${LOCAL_RANK} --world-size ${NPROC} --kv-shape \"${KV_SHAPE}\" --kvcache-shape-spec \"${KVCACHE_SPEC}\" --kv-dtype float16 --device cuda:\${LOCAL_RANK}"

    echo "[multi_gpu] launching distributed run..."
    "$TARGET_PYTHON" -m torch.distributed.run \
      --standalone \
      --nproc_per_node="$NPROC" \
      --no_python \
      bash -lc "$inner_cmd"
  ' bash "{python}"
