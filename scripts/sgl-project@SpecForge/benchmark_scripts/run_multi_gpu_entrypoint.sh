#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU (1-step) training via the repository entrypoint.

Defaults (override via env vars):
  SPECFORGE_BENCH_MULTI_GPU_VISIBLE_DEVICES=0,1
  SPECFORGE_BENCH_MULTI_GPU_NPROC=2

Optional:
  --python <path>                Override python executable used for torchrun
  --report-path <path>           Override report.json path for python resolution
EOF
}

python_bin=""
report_path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

host_python="$(command -v python3 || command -v python || true)"
if [[ -z "$host_python" ]]; then
  mkdir -p build_output/multi_gpu
  cat > build_output/multi_gpu/log.txt <<'EOF'
[multi_gpu] ERROR: python3/python not found in PATH
EOF
  cat > build_output/multi_gpu/results.json <<'EOF'
{"status":"failure","skip_reason":"not_applicable","exit_code":1,"stage":"multi_gpu","task":"train","command":"","timeout_sec":1200,"framework":"pytorch","assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},"meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"host python not found"},"failure_category":"deps","error_excerpt":"host python not found"}
EOF
  exit 1
fi

visible_devices="${SPECFORGE_BENCH_MULTI_GPU_VISIBLE_DEVICES:-0,1}"
nproc="${SPECFORGE_BENCH_MULTI_GPU_NPROC:-2}"

resolved_python="$python_bin"
if [[ -z "$resolved_python" ]]; then
  runner_print_args=(--stage multi_gpu --task train --print-resolved-python)
  if [[ -n "$report_path" ]]; then
    runner_print_args+=(--report-path "$report_path")
  fi
  resolved_python="$("$host_python" benchmark_scripts/runner.py "${runner_print_args[@]}" 2>/dev/null || true)"
fi

if [[ -z "$resolved_python" ]]; then
  runner_fail_args=(--stage multi_gpu --task train --framework pytorch)
  if [[ -n "$report_path" ]]; then
    runner_fail_args+=(--report-path "$report_path")
  fi
  "$host_python" benchmark_scripts/runner.py "${runner_fail_args[@]}"
  exit 1
fi

gpu_count="$("$resolved_python" - <<'PY' 2>/dev/null || echo 0
try:
    import torch
    print(torch.cuda.device_count())
except Exception:
    print(0)
PY
)"
gpu_count="${gpu_count//[[:space:]]/}"
gpu_count="${gpu_count:-0}"

if [[ "$gpu_count" -lt 2 ]]; then
  # Requirement: GPU count <2 => exit 1.
  "$host_python" benchmark_scripts/runner.py \
    --stage multi_gpu --task train --framework pytorch --python "$resolved_python" \
    --failure-category runtime \
    --shell-cmd "echo '[multi_gpu] ERROR: need >=2 GPUs for this stage (observed gpu_count=$gpu_count)'; exit 1"
  exit 1
fi

prepare_results="build_output/prepare/results.json"
if [[ ! -f "$prepare_results" ]]; then
  "$host_python" benchmark_scripts/runner.py \
    --stage multi_gpu --task train --framework pytorch --python "$resolved_python" \
    --failure-category data \
    --shell-cmd "echo '[multi_gpu] ERROR: missing $prepare_results (run prepare_assets.sh first)'; exit 1"
  exit 1
fi

read_prepare_field() {
  "$host_python" - "$prepare_results" "$1" <<'PY' 2>/dev/null || return 1
import json, sys
path = sys.argv[1]
key = sys.argv[2]
data = json.load(open(path, "r", encoding="utf-8"))
cur = data
for part in key.split("."):
    if not isinstance(cur, dict) or part not in cur:
        sys.exit(1)
    cur = cur[part]
if isinstance(cur, (str, int, float, bool)) or cur is None:
    print("" if cur is None else cur)
else:
    print(json.dumps(cur))
PY
}

dataset_path="$(read_prepare_field "assets.dataset.path" || true)"
dataset_source="$(read_prepare_field "assets.dataset.source" || true)"
dataset_version="$(read_prepare_field "assets.dataset.version" || true)"
dataset_sha256="$(read_prepare_field "assets.dataset.sha256" || true)"
model_path="$(read_prepare_field "assets.model.path" || true)"
model_source="$(read_prepare_field "assets.model.source" || true)"
model_version="$(read_prepare_field "assets.model.version" || true)"
model_sha256="$(read_prepare_field "assets.model.sha256" || true)"
draft_cfg_path="$(read_prepare_field "meta.draft_model_config_path" || true)"

if [[ -z "$dataset_path" || -z "$model_path" || -z "$draft_cfg_path" ]]; then
  "$host_python" benchmark_scripts/runner.py \
    --stage multi_gpu --task train --framework pytorch --python "$resolved_python" \
    --failure-category data \
    --shell-cmd "echo '[multi_gpu] ERROR: prepare stage did not provide dataset/model/draft_config paths'; exit 1"
  exit 1
fi

cache_root="$repo_root/benchmark_assets/cache"
specforge_cache="$cache_root/specforge_cache"
mkdir -p "$specforge_cache"

decision_reason="Official entrypoint: torchrun scripts/train_eagle3.py (per examples/). Multi-GPU uses torch.distributed.run with nproc_per_node=$nproc and CUDA_VISIBLE_DEVICES=$visible_devices; minimal 1-step run."

"$host_python" benchmark_scripts/runner.py \
  --stage multi_gpu \
  --task train \
  --framework pytorch \
  --timeout-sec 1200 \
  --python "$resolved_python" \
  --decision-reason "$decision_reason" \
  --dataset-path "$dataset_path" --dataset-source "$dataset_source" --dataset-version "$dataset_version" --dataset-sha256 "$dataset_sha256" \
  --model-path "$model_path" --model-source "$model_source" --model-version "$model_version" --model-sha256 "$model_sha256" \
  --env "CUDA_VISIBLE_DEVICES=$visible_devices" \
  --env "HF_HOME=$cache_root/huggingface" \
  --env "HF_HUB_CACHE=$cache_root/huggingface/hub" \
  --env "HF_DATASETS_CACHE=$cache_root/huggingface/datasets" \
  --env "TRANSFORMERS_CACHE=$cache_root/huggingface/transformers" \
  --env "XDG_CACHE_HOME=$cache_root/xdg" \
  --env "TORCH_HOME=$cache_root/torch" \
  --env "TORCHINDUCTOR_CACHE_DIR=$cache_root/torchinductor" \
  --env "PYTHONPYCACHEPREFIX=$cache_root/pycache" \
  --env "WANDB_MODE=disabled" \
  --env "WANDB_DIR=$cache_root/wandb" \
  --env "HF_HUB_DISABLE_TELEMETRY=1" \
  --env "HF_DATASETS_DISABLE_TELEMETRY=1" \
  -- "$resolved_python" -m torch.distributed.run \
      --standalone \
      --nproc_per_node "$nproc" \
      scripts/train_eagle3.py \
      --target-model-path "$model_path" \
      --draft-model-config "$draft_cfg_path" \
      --train-data-path "$dataset_path" \
      --build-dataset-num-proc 1 \
      --dataloader-num-workers 0 \
      --output-dir "$repo_root/build_output/multi_gpu/output" \
      --num-epochs 1 \
      --max-num-steps 1 \
      --batch-size 1 \
      --tp-size 1 \
      --learning-rate 1e-4 \
      --max-length 256 \
      --chat-template qwen \
      --cache-dir "$specforge_cache" \
      --attention-backend sdpa \
      --target-model-backend hf \
      --embedding-key model.embed_tokens.weight \
      --eval-interval 1000000000 \
      --save-interval 1000000000 \
      --log-interval 1 \
      --model-download-dir "$cache_root/huggingface/hub"

