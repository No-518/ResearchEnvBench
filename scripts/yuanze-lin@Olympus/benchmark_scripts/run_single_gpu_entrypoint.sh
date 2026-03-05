#!/usr/bin/env bash
set -u
set -o pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step single-GPU training run via the repository entrypoint.

Entrypoint:
  mipha/train/train.py

Outputs:
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

Behavior:
  - Forces CUDA_VISIBLE_DEVICES=0
  - batch_size=1, max_steps=1

Optional:
  --python <path>        Override python executable (highest priority)
  --report-path <path>   Override report.json path (default: /opt/scimlopsbench/report.json)
  --timeout-sec <n>      Override timeout (default: 600)
EOF
}

python_override=""
report_path=""
timeout_sec="600"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_override="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

runner_py=""
if command -v python3 >/dev/null 2>&1; then runner_py="python3"; fi
if [[ -z "$runner_py" ]] && command -v python >/dev/null 2>&1; then runner_py="python"; fi
if [[ -z "$runner_py" ]]; then
  mkdir -p build_output/single_gpu
  cat > build_output/single_gpu/log.txt <<'EOF'
No python/python3 found in PATH to run benchmark_scripts/runner.py.
EOF
  cat > build_output/single_gpu/results.json <<'EOF'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "single_gpu",
  "task": "train",
  "command": "",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "runner python missing"},
  "failure_category": "deps",
  "error_excerpt": "missing python"
}
EOF
  exit 1
fi

resolved_python="$("$runner_py" benchmark_scripts/runner.py --stage single_gpu --task train --requires-python --print-resolved-python ${python_override:+--python "$python_override"} ${report_path:+--report-path "$report_path"} 2>/dev/null || true)"

manifest="benchmark_assets/manifest.json"
dataset_path=""
model_path=""
if [[ -f "$manifest" ]]; then
  dataset_path="$("$runner_py" - <<'PY' "$manifest"
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
data = json.loads(p.read_text(encoding="utf-8"))
ds = data.get("dataset", {})
print(ds.get("path", ""))
PY
  )"
  model_path="$("$runner_py" - <<'PY' "$manifest"
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
data = json.loads(p.read_text(encoding="utf-8"))
md = data.get("model", {})
print(md.get("path", ""))
PY
  )"
fi

if [[ -z "$dataset_path" || -z "$model_path" ]]; then
  "$runner_py" benchmark_scripts/runner.py \
    --stage single_gpu --task train --framework pytorch --requires-python \
    ${python_override:+--python "$python_override"} ${report_path:+--report-path "$report_path"} \
    --timeout-sec "$timeout_sec" --failure-category data \
    --decision-reason "prepare_assets.sh did not produce benchmark_assets/manifest.json with dataset/model paths" \
    -- bash -lc 'echo "Missing dataset/model paths; run benchmark_scripts/prepare_assets.sh first." >&2; exit 1'
  exit $?
fi

if [[ -n "$resolved_python" ]]; then
  gpu_count="$("$resolved_python" - <<'PY' 2>/dev/null || echo "0"
try:
    import torch
    print(torch.cuda.device_count())
except Exception:
    print(0)
PY
  )"
else
  gpu_count="0"
fi

if [[ "${gpu_count:-0}" -lt 1 ]]; then
  CUDA_VISIBLE_DEVICES=0 \
    "$runner_py" benchmark_scripts/runner.py \
      --stage single_gpu --task train --framework pytorch --requires-python \
      ${python_override:+--python "$python_override"} ${report_path:+--report-path "$report_path"} \
      --timeout-sec "$timeout_sec" --failure-category runtime \
      --decision-reason "No GPU detected via torch.cuda.device_count(); cannot run single-GPU stage." \
      -- bash -lc 'echo "No CUDA GPU available (need >=1)." >&2; exit 1'
  exit $?
fi

export HF_HOME="benchmark_assets/cache/hf_home"
export TRANSFORMERS_CACHE="benchmark_assets/cache/transformers_cache"
export HF_DATASETS_CACHE="benchmark_assets/cache/hf_datasets_cache"
export TORCH_HOME="benchmark_assets/cache/torch_cache"
export TORCH_EXTENSIONS_DIR="$repo_root/benchmark_assets/cache/torch_extensions"
export WANDB_DISABLED="true"
export WANDB_MODE="disabled"
export WANDB_DIR="$repo_root/build_output/single_gpu/wandb"
export WANDB_CONFIG_DIR="$repo_root/build_output/single_gpu/wandb_config"
export WANDB_CACHE_DIR="$repo_root/build_output/single_gpu/wandb_cache"
export WANDB_DATA_DIR="$repo_root/build_output/single_gpu/wandb_data"
export TMPDIR="$repo_root/build_output/single_gpu/tmp"
export PIP_CACHE_DIR="$repo_root/benchmark_assets/cache/pip_cache"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg_cache"
export XDG_CONFIG_HOME="$repo_root/benchmark_assets/cache/xdg_config"
export XDG_DATA_HOME="$repo_root/benchmark_assets/cache/xdg_data"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$repo_root/build_output/single_gpu/pycache"
mkdir -p "$WANDB_DIR" "$WANDB_CONFIG_DIR" "$WANDB_CACHE_DIR" "$WANDB_DATA_DIR" "$TMPDIR" "$PIP_CACHE_DIR" \
  "$TORCH_EXTENSIONS_DIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$PYTHONPYCACHEPREFIX"
export TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"

out_artifacts="build_output/single_gpu/artifacts"
mkdir -p "$out_artifacts"

dataset_abs="$repo_root/$dataset_path"
model_abs="$repo_root/$model_path"

CUDA_VISIBLE_DEVICES=0 \
  "$runner_py" benchmark_scripts/runner.py \
    --stage single_gpu --task train --framework pytorch --requires-python \
    ${python_override:+--python "$python_override"} ${report_path:+--report-path "$report_path"} \
    --timeout-sec "$timeout_sec" \
    --decision-reason "Run mipha/train/train.py for exactly 1 step (max_steps=1, batch_size=1) on a single GPU using prepared minimal dataset and model." \
    --use-python \
    -- \
    mipha/train/train.py \
      --model_name_or_path "$model_abs" \
      --version v0 \
      --data_path "$dataset_abs" \
      --image_folder "$repo_root/benchmark_assets/dataset" \
      --tune_mm_mlp_adapter True \
      --freeze_vision_tower True \
      --freeze_backbone True \
      --mm_use_im_start_end False \
      --mm_use_im_patch_token False \
      --image_aspect_ratio pad \
      --group_by_modality_length False \
      --bf16 False \
      --fp16 True \
      --output_dir "$repo_root/$out_artifacts" \
      --overwrite_output_dir True \
      --max_steps 1 \
      --per_device_train_batch_size 1 \
      --gradient_accumulation_steps 1 \
      --evaluation_strategy no \
      --save_strategy no \
      --learning_rate 1e-6 \
      --logging_steps 1 \
      --tf32 False \
      --model_max_length 128 \
      --gradient_checkpointing False \
      --dataloader_num_workers 0 \
      --lazy_preprocess True \
      --report_to none \
      --run_name scimlopsbench_single_gpu
