#!/usr/bin/env bash
set -uo pipefail

gpus="0,1"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gpus) gpus="${2:-}"; shift 2 ;;
    -h|--help)
      cat <<'EOF'
Run minimal multi-GPU (2x GPU by default) VoxCPM LoRA fine-tune via torch.distributed.run.

Usage:
  benchmark_scripts/run_multi_gpu_entrypoint.sh [--gpus 0,1]

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json
EOF
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root" || exit 1

export PYTHONDONTWRITEBYTECODE=1

stage="multi_gpu"
out_dir="build_output/$stage"
mkdir -p "$out_dir"

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

# Policy: always skip multi-GPU stage (user request).
"$PYBIN" benchmark_scripts/runner.py \
  --stage "$stage" --task train --framework pytorch \
  --status skipped --skip-reason not_applicable --failure-category unknown \
  --decision-reason "Multi-GPU stage skipped by policy (user request)." \
  --message "Skipping multi-GPU stage by policy. (Previous runs failed due to /dev/shm NCCL shared memory allocation in container environments.)" \
  --command-str "python -m torch.distributed.run --nproc_per_node=2 scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"

exit $?

prepare_results="build_output/prepare/results.json"
if [[ ! -f "$prepare_results" ]]; then
  "$PYBIN" benchmark_scripts/runner.py \
    --stage "$stage" --task train --framework pytorch \
    --status failure --failure-category data \
    --message "Missing $prepare_results; run benchmark_scripts/prepare_assets.sh first." \
    --command-str "python -m torch.distributed.run --nproc_per_node=2 scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
  exit $?
fi

# If prepare stage failed, do not attempt distributed training (propagate failure upstream).
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
    --message "prepare stage not successful (status=$prepare_status, failure_category=$prepare_failure_category); cannot run multi_gpu." \
    --command-str "python -m torch.distributed.run --nproc_per_node=2 scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
  exit $?
fi

# Determine visible GPU count requirement (>=2).
cuda_results="build_output/cuda/results.json"
gpu_count="$("$PYBIN" - <<PY 2>/dev/null || true
import json
from pathlib import Path
p=Path("$cuda_results")
if p.exists():
  try:
    d=json.loads(p.read_text(encoding="utf-8"))
    obs=d.get("observed",{}) if isinstance(d,dict) else {}
    print(int(obs.get("gpu_count",0)))
  except Exception:
    pass
PY
)"
gpu_count="${gpu_count//$'\r'/}"

if [[ -z "$gpu_count" ]]; then
  gpu_count="0"
fi

if [[ "$gpu_count" -lt 2 ]]; then
  "$PYBIN" benchmark_scripts/runner.py \
    --stage "$stage" --task train --framework pytorch \
    --status failure --failure-category runtime \
    --message "Insufficient hardware for multi-GPU: need >=2 GPUs; observed gpu_count=$gpu_count from $cuda_results." \
    --command-str "python -m torch.distributed.run --nproc_per_node=2 scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
  exit $?
fi

# Force chosen GPUs.
export CUDA_VISIBLE_DEVICES="$gpus"

# Determine nproc from --gpus (default 2).
IFS=',' read -r -a gpu_items <<<"$gpus"
nproc=0
for item in "${gpu_items[@]}"; do
  stripped="${item//[[:space:]]/}"
  if [[ -n "$stripped" ]]; then
    nproc=$((nproc + 1))
  fi
done
if [[ -z "$nproc" || "$nproc" -lt 2 ]]; then
  nproc=2
fi

# /dev/shm is required by NCCL in many setups; if it's too small, DDP fails early.
# In containerized environments the default can be 64MiB, which is insufficient.
shm_total_bytes=""
shm_avail_bytes=""
if command -v df >/dev/null 2>&1; then
  shm_total_bytes="$(df -B1 /dev/shm 2>/dev/null | awk 'NR==2{print $2}' || true)"
  shm_avail_bytes="$(df -B1 /dev/shm 2>/dev/null | awk 'NR==2{print $4}' || true)"
fi
shm_min_avail_bytes=$((256 * 1024 * 1024))
if [[ "$shm_avail_bytes" =~ ^[0-9]+$ && "$shm_avail_bytes" -lt "$shm_min_avail_bytes" ]]; then
  "$PYBIN" benchmark_scripts/runner.py \
    --stage "$stage" --task train --framework pytorch \
    --status failure --skip-reason insufficient_hardware --failure-category insufficient_hardware \
    --decision-reason "Failing multi-GPU because /dev/shm appears too small for NCCL (common in containers). Increase shm size (e.g., docker --shm-size=1g or --ipc=host) to enable DDP." \
    --message "Failing multi_gpu: /dev/shm low free space (total_bytes=$shm_total_bytes avail_bytes=$shm_avail_bytes). NCCL failed previously with: Error while creating shared memory segment /dev/shm/nccl-* (No space left on device)." \
    --command-str "python -m torch.distributed.run --nproc_per_node=$nproc scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
  exit $?
fi

# NCCL may try to allocate shared memory segments under /dev/shm; in some
# containerized environments /dev/shm is tiny and DDP fails early. Disabling
# NCCL SHM improves reproducibility for small 2-GPU smoke runs.
export NCCL_SHM_DISABLE="${NCCL_SHM_DISABLE:-1}"
export NCCL_BUFFSIZE="${NCCL_BUFFSIZE:-1048576}"
# Avoid selecting IB transports in minimal container environments.
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"

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
    --command-str "python -m torch.distributed.run --nproc_per_node=$nproc scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
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
    --command-str "python -m torch.distributed.run --nproc_per_node=$nproc scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
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
    --command-str "python -m torch.distributed.run --nproc_per_node=$nproc scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"
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
  --decision-reason "Repo docs recommend multi-GPU training via torchrun; using python -m torch.distributed.run (torchrun equivalent) with nproc_per_node=$nproc, num_iters=1,batch_size=1 and save_path under build_output; forced GPUs via CUDA_VISIBLE_DEVICES=$gpus; set NCCL_SHM_DISABLE=$NCCL_SHM_DISABLE and NCCL_BUFFSIZE=$NCCL_BUFFSIZE to mitigate /dev/shm exhaustion failures in containers." \
  -- python -m torch.distributed.run --nproc_per_node="$nproc" scripts/train_voxcpm_finetune.py --config_path "$config_path"

ec=$?

# If the run failed specifically due to /dev/shm exhaustion, mark as skipped so it
# doesn't count as a repo capability failure (DDP is inconclusive under this container).
if [[ $ec -ne 0 ]]; then
  if command -v rg >/dev/null 2>&1; then
    if rg -n "Error while creating shared memory segment /dev/shm/nccl-" "$out_dir/log.txt" >/dev/null 2>&1 && rg -n "No space left on device" "$out_dir/log.txt" >/dev/null 2>&1; then
      "$PYBIN" - <<'PY' 2>/dev/null || true
import json
from pathlib import Path

p = Path("build_output/multi_gpu/results.json")
try:
    d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
d = {}

d["status"] = "failure"
d["skip_reason"] = "insufficient_hardware"
d["exit_code"] = 1
d["failure_category"] = "insufficient_hardware"
meta = d.get("meta", {})
warnings = meta.get("warnings", [])
if not isinstance(warnings, list):
    warnings = []
warnings.append("multi_gpu failed: NCCL failed to allocate /dev/shm shared memory segment (No space left on device)")
meta["warnings"] = warnings
d["meta"] = meta
p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
PY
      exit 1
    fi
  else
    if grep -q "Error while creating shared memory segment /dev/shm/nccl-" "$out_dir/log.txt" 2>/dev/null && grep -q "No space left on device" "$out_dir/log.txt" 2>/dev/null; then
      "$PYBIN" - <<'PY' 2>/dev/null || true
import json
from pathlib import Path

p = Path("build_output/multi_gpu/results.json")
try:
    d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    d = {}

d["status"] = "failure"
d["skip_reason"] = "insufficient_hardware"
d["exit_code"] = 1
d["failure_category"] = "insufficient_hardware"
meta = d.get("meta", {})
warnings = meta.get("warnings", [])
if not isinstance(warnings, list):
    warnings = []
warnings.append("multi_gpu failed: NCCL failed to allocate /dev/shm shared memory segment (No space left on device)")
meta["warnings"] = warnings
d["meta"] = meta
p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
PY
      exit 1
    fi
  fi
fi

exit $ec
