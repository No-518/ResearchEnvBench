#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root" || exit 1

export PYTHONDONTWRITEBYTECODE=1

stage="single_gpu"
out_dir="build_output/$stage"
mkdir -p "$out_dir"

# Force single GPU.
export CUDA_VISIBLE_DEVICES="0"

# Redirect common caches into allowed tree.
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HOME="$repo_root/benchmark_assets/cache/hf_home"
export HF_HUB_CACHE="$repo_root/benchmark_assets/cache/hf_hub"
export HUGGINGFACE_HUB_CACHE="$repo_root/benchmark_assets/cache/hf_hub"
export HF_DATASETS_CACHE="$repo_root/benchmark_assets/cache/hf_datasets"
export TRANSFORMERS_CACHE="$repo_root/benchmark_assets/cache/hf_transformers"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export MPLCONFIGDIR="$repo_root/benchmark_assets/cache/matplotlib"
export PIP_CACHE_DIR="$repo_root/benchmark_assets/cache/pip"
export PIP_DISABLE_PIP_VERSION_CHECK=1

# Best-effort: set SCIMLOPSBENCH_PYTHON from report.json if available.
if [[ -z "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
  if [[ -n "$report_path" && -d "$report_path" ]]; then
    report_path="$report_path/report.json"
  fi
  pyhost="$(command -v python3 || command -v python || true)"
  if [[ -n "$pyhost" && -f "$report_path" ]]; then
    set +u
    resolved="$("$pyhost" - <<'PY' 2>/dev/null || true
import json
import os
from pathlib import Path

raw = os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"
p = Path(raw)
if p.is_dir():
    p = p / "report.json"
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("python_path", ""))
except Exception:
    print("")
PY
)"
    set -u
    resolved="${resolved//$'\r'/}"
    if [[ -n "$resolved" ]]; then
      export SCIMLOPSBENCH_PYTHON="$resolved"
    fi
  fi
fi

PYBIN="${SCIMLOPSBENCH_PYTHON:-}"
if [[ -n "$PYBIN" && ! -x "$PYBIN" ]]; then
  PYBIN=""
fi
if [[ -z "$PYBIN" ]]; then
  PYBIN="$(command -v python3 || command -v python)"
fi

prepare_results="build_output/prepare/results.json"
if [[ ! -f "$prepare_results" ]]; then
  "$PYBIN" benchmark_scripts/runner.py \
    --stage "$stage" --task train --framework pytorch \
    --status failure --failure-category data \
    --message "Missing $prepare_results; run benchmark_scripts/prepare_assets.sh first." \
    --command-str "python scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
  exit $?
fi

# If prepare stage failed, do not attempt training (propagate failure upstream).
prep_status="$("$PYBIN" - <<PY 2>/dev/null || true
import json
from pathlib import Path
d=json.loads(Path("$prepare_results").read_text(encoding="utf-8"))
print(str(d.get("status","failure")))
print(str(d.get("failure_category","unknown")))
PY
)"
prepare_status="$(printf "%s" "$prep_status" | sed -n '1p')"
prepare_failure_category="$(printf "%s" "$prep_status" | sed -n '2p')"

if [[ "$prepare_status" != "success" ]]; then
  "$PYBIN" benchmark_scripts/runner.py \
    --stage "$stage" --task train --framework pytorch \
    --status failure --failure-category "${prepare_failure_category:-unknown}" \
    --message "prepare stage not successful (status=$prepare_status, failure_category=$prepare_failure_category); cannot run single_gpu." \
    --command-str "python scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
  exit $?
fi

read_vars="$("$PYBIN" - <<PY 2>/dev/null || true
import json
from pathlib import Path
d=json.loads(Path("$prepare_results").read_text(encoding="utf-8"))
assets=d.get("assets",{})
meta=d.get("meta",{}).get("prepared",{})
print(assets.get("dataset",{}).get("path",""))
print(assets.get("model",{}).get("path",""))
print(int(meta.get("sample_rate",16000)))
PY
)"

dataset_manifest="$(printf "%s" "$read_vars" | sed -n '1p')"
model_path="$(printf "%s" "$read_vars" | sed -n '2p')"
sample_rate="$(printf "%s" "$read_vars" | sed -n '3p')"

if [[ -z "$dataset_manifest" || -z "$model_path" ]]; then
  "$PYBIN" benchmark_scripts/runner.py \
    --stage "$stage" --task train --framework pytorch \
    --status failure --failure-category data \
    --message "prepare results.json is missing assets.dataset.path or assets.model.path" \
    --command-str "python scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
  exit $?
fi

dataset_abs="$dataset_manifest"
if [[ "$dataset_abs" != /* ]]; then
  dataset_abs="$repo_root/$dataset_abs"
fi
if [[ ! -f "$dataset_abs" ]]; then
  "$PYBIN" benchmark_scripts/runner.py \
    --stage "$stage" --task train --framework pytorch \
    --status failure --failure-category data \
    --message "Dataset manifest not found: $dataset_manifest" \
    --command-str "python scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
  exit $?
fi

model_abs="$model_path"
if [[ "$model_abs" != /* ]]; then
  model_abs="$repo_root/$model_abs"
fi
if [[ ! -d "$model_abs" || ! -f "$model_abs/config.json" || ! -f "$model_abs/audiovae.pth" ]]; then
  "$PYBIN" benchmark_scripts/runner.py \
    --stage "$stage" --task train --framework pytorch \
    --status failure --failure-category model \
    --message "Model directory is incomplete: $model_path (need config.json + audiovae.pth)" \
    --command-str "python scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
  exit $?
fi

config_path="$out_dir/config.yaml"
cat >"$config_path" <<EOF
pretrained_path: $model_path
train_manifest: $dataset_manifest
val_manifest: ""

sample_rate: $sample_rate
batch_size: 1
grad_accum_steps: 1
num_workers: 0
num_iters: 1
log_interval: 1
valid_interval: 1000000
save_interval: 1000000

learning_rate: 0.0001
weight_decay: 0.01
warmup_steps: 0
max_steps: 1
max_batch_tokens: 0

save_path: $out_dir/checkpoints
tensorboard: $out_dir/tensorboard

lambdas:
  loss/diff: 1.0
  loss/stop: 1.0

lora:
  enable_lm: true
  enable_dit: true
  enable_proj: false
  r: 4
  alpha: 4
  dropout: 0.0
EOF

"$PYBIN" benchmark_scripts/runner.py \
  --stage "$stage" --task train --framework pytorch \
  --decision-reason "Single-GPU minimal LoRA fine-tune via scripts/train_voxcpm_finetune.py with num_iters=1,batch_size=1; forced GPU 0 via CUDA_VISIBLE_DEVICES=0; save_path under build_output." \
  -- python scripts/train_voxcpm_finetune.py --config_path "$config_path"

exit $?
