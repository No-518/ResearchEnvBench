#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run minimal multi-GPU training via the repository entrypoint.

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Behavior:
  - Requires >=2 GPUs; if fewer, exits 1 (failure).

Optional:
  --python <path>             Override python interpreter
  --report-path <path>        Override report path
  --cuda-visible-devices <s>  Override CUDA_VISIBLE_DEVICES (default: 0,1)
EOF
}

python_bin=""
report_path=""
cuda_visible_devices="0,1"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --cuda-visible-devices) cuda_visible_devices="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
stage="multi_gpu"
stage_dir="$repo_root/build_output/$stage"
mkdir -p "$stage_dir"

assets_from="$repo_root/build_output/prepare/results.json"
if [[ ! -f "$assets_from" ]]; then
  python3 "$script_dir/runner.py" run \
    --stage "$stage" --task train --timeout-sec 1200 --framework pytorch \
    --no-run --status failure --failure-category data \
    --error "missing prepare assets results: $assets_from" \
    -- "true"
  exit 1
fi

resolver=(python3 "$script_dir/runner.py" resolve-python)
if [[ -n "$python_bin" ]]; then
  resolver+=(--python "$python_bin")
fi
if [[ -n "$report_path" ]]; then
  resolver+=(--report-path "$report_path")
fi
py_bin="$("${resolver[@]}")" || true
if [[ -z "${py_bin:-}" ]]; then
  python3 "$script_dir/runner.py" run \
    --stage "$stage" --task train --timeout-sec 1200 --framework pytorch \
    --no-run --status failure --failure-category missing_report \
    --error "failed to resolve python from report for multi_gpu stage" \
    -- "true"
  exit 1
fi

export CUDA_VISIBLE_DEVICES="$cuda_visible_devices"

gpu_count="$("$py_bin" - <<'PY'
try:
    import torch
    print(torch.cuda.device_count())
except Exception:
    print(0)
PY
)"
if [[ "${gpu_count:-0}" -lt 2 ]]; then
  python3 "$script_dir/runner.py" run \
    --stage "$stage" --task train --timeout-sec 1200 --framework pytorch --assets-from "$assets_from" \
    --no-run --status failure --failure-category runtime \
    --error "Need >=2 GPUs for multi-GPU stage; detected gpu_count=$gpu_count" \
    -- "true"
  exit 1
fi

devices="$gpu_count"
batch_size="1" # per-device batch size is set via loader_args.batch_size=1

dataset_path="$("$py_bin" - <<PY
import json, pathlib
p = pathlib.Path("$assets_from")
data = json.loads(p.read_text(encoding="utf-8"))
print(data.get("assets", {}).get("dataset", {}).get("path", ""))
PY
)"
model_path="$("$py_bin" - <<PY
import json, pathlib
p = pathlib.Path("$assets_from")
data = json.loads(p.read_text(encoding="utf-8"))
print(data.get("assets", {}).get("model", {}).get("path", ""))
PY
)"

out_run_dir="$stage_dir/run"

export LIGHTLY_TRAIN_EVENTS_DISABLED=1
export LIGHTLY_TRAIN_CACHE_DIR="$repo_root/benchmark_assets/cache/lightly-train"
export LIGHTLY_TRAIN_MODEL_CACHE_DIR="$model_path"
export LIGHTLY_TRAIN_DATA_CACHE_DIR="$repo_root/benchmark_assets/cache/lightly-train/data"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HOME="$repo_root/benchmark_assets/cache/hf"
export TRANSFORMERS_CACHE="$repo_root/benchmark_assets/cache/hf/transformers"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"

entrypoint=("$py_bin" -c "from lightly_train._cli import _cli_entrypoint; _cli_entrypoint()")
lightly_train_cli="$(dirname "$py_bin")/lightly-train"
if [[ -x "$lightly_train_cli" ]]; then
  entrypoint=("$lightly_train_cli")
fi

cmd=(
  "${entrypoint[@]}"
  pretrain
  "out=$out_run_dir"
  "overwrite=true"
  "data=$dataset_path"
  "model=dinov3/vitt16"
  "method=simclr"
  "epochs=1"
  "batch_size=$batch_size"
  "loader_args.batch_size=1"
  "num_workers=0"
  "accelerator=gpu"
  "devices=$devices"
  "strategy=ddp"
  "trainer_args.max_steps=1"
  "trainer_args.limit_train_batches=1"
)

runner_cmd=(python3 "$script_dir/runner.py" run)
runner_cmd+=(--stage "$stage" --task train --timeout-sec 1200 --framework pytorch --assets-from "$assets_from")
runner_cmd+=(
  --decision-reason
  "Using lightly_train CLI entrypoint (_cli_entrypoint) with pretrain; multi-GPU per docs/source/performance/multi_gpu.md via accelerator=gpu devices=$devices (DDP). LightlyTrain expects global batch_size divisible by devices; setting loader_args.batch_size=1 enforces per-device batch size=1 and lets LightlyTrain derive a valid global batch size. Limiting to 1 step."
)
runner_cmd+=(--require-python)
if [[ -n "$python_bin" ]]; then
  runner_cmd+=(--python "$python_bin")
fi
if [[ -n "$report_path" ]]; then
  runner_cmd+=(--report-path "$report_path")
fi
runner_cmd+=(-- "${cmd[@]}")
"${runner_cmd[@]}"
