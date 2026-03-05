#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step multi-GPU (DDP) training via the repository's native entrypoint.

Entrypoint (repo):
  train/speech_enhancement/train.py (FRCRN_SE_16K)

Distributed launcher:
  python -m torch.distributed.run (torchrun equivalent)

Requires:
  build_output/prepare/results.json (from benchmark_scripts/prepare_assets.sh)

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Options:
  --python <path>          Override python (otherwise uses report.json)
  --cuda-devices <csv>     Default: 0,1
  --nproc <int>            Default: 2
  --timeout-sec <sec>      Default: 1200
EOF
}

python_override=""
cuda_devices="0,1"
nproc="2"
timeout_sec="1200"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_override="${2:-}"; shift 2 ;;
    --cuda-devices)
      cuda_devices="${2:-}"; shift 2 ;;
    --nproc)
      nproc="${2:-}"; shift 2 ;;
    --timeout-sec)
      timeout_sec="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/multi_gpu"
log_path="$stage_dir/log.txt"
results_path="$stage_dir/results.json"

mkdir -p "$repo_root/benchmark_assets/cache/pycache" "$repo_root/benchmark_assets/cache/xdg" "$repo_root/benchmark_assets/cache/torch" "$repo_root/benchmark_assets/cache/hf"
export PYTHONPYCACHEPREFIX="$repo_root/benchmark_assets/cache/pycache"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export HF_HOME="$repo_root/benchmark_assets/cache/hf"
export HUGGINGFACE_HUB_CACHE="$repo_root/benchmark_assets/cache/hf"

mkdir -p "$stage_dir"
: >"$log_path"

note() { echo "[multi_gpu] $*" >>"$log_path"; }

write_results() {
  STAGE_STATUS="$1" EXIT_CODE="$2" FAILURE_CATEGORY="$3" SKIP_REASON="$4" COMMAND_STR="$5" DECISION_REASON="$6" \
  PYTHON_EXE="$7" \
  LOG_PATH="$log_path" RESULTS_PATH="$results_path" \
  python - <<'PY'
import json
import os
import pathlib
import time

def tail(path: str, n: int = 220) -> str:
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

payload = {
    "status": os.environ.get("STAGE_STATUS", "failure"),
    "skip_reason": os.environ.get("SKIP_REASON", "unknown"),
    "exit_code": int(os.environ.get("EXIT_CODE", "1")),
    "stage": "multi_gpu",
    "task": "train",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 1200,
    "framework": "pytorch",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PYTHON_EXE", ""),
        "git_commit": "",
        "env_vars": {"CUDA_VISIBLE_DEVICES": os.getenv("CUDA_VISIBLE_DEVICES", "")},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail(os.environ.get("LOG_PATH", "")),
}

pathlib.Path(os.environ["RESULTS_PATH"]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY
}

report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_exe=""
if [[ -n "$python_override" ]]; then
  python_exe="$python_override"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  python_exe="$SCIMLOPSBENCH_PYTHON"
elif [[ -f "$report_path" ]]; then
  python_exe="$(python - <<PY 2>>"$log_path" || true
import json
try:
  data=json.load(open("${report_path}","r",encoding="utf-8"))
  print(data.get("python_path","") or "")
except Exception:
  print("")
PY
)"
fi

note "report_path=$report_path"
note "python_exe=${python_exe:-<empty>}"

if [[ -z "$python_exe" ]]; then
  write_results "failure" 1 "missing_report" "unknown" "" "Could not resolve python from report.json (or overrides)" ""
  exit 1
fi

if ! "$python_exe" -c 'import sys; print(sys.executable)' >>"$log_path" 2>&1; then
  write_results "failure" 1 "deps" "unknown" "" "Resolved python is not executable" "$python_exe"
  exit 1
fi

# GPU availability check (need >=2 by default, and >= nproc for requested nproc).
gpu_count="$(
  CUDA_VISIBLE_DEVICES="${cuda_devices}" "$python_exe" - <<'PY' 2>>"$log_path" || true
try:
  import torch
  print(torch.cuda.device_count())
except Exception:
  print("")
PY
)"
if [[ -z "$gpu_count" ]]; then
  note "torch import/device_count failed"
  write_results "failure" 1 "deps" "unknown" "" "torch unavailable; cannot determine GPU count" "$python_exe"
  exit 1
fi
note "detected_gpu_count=$gpu_count"

if [[ "$gpu_count" -lt 2 || "$gpu_count" -lt "$nproc" ]]; then
  note "Insufficient GPUs for multi-GPU run: need >=2 and >=nproc=$nproc"
  write_results "failure" 1 "runtime" "insufficient_hardware" "" "Insufficient GPUs for multi-GPU run" "$python_exe"
  exit 1
fi

prepare_results="$repo_root/build_output/prepare/results.json"
dataset_root="$repo_root/benchmark_assets/dataset"
train_scp="$dataset_root/train.scp"
cv_scp="$dataset_root/cv.scp"

if [[ ! -f "$prepare_results" ]]; then
  note "Missing prepare results: $prepare_results"
  write_results "failure" 1 "data" "unknown" "" "prepare stage results.json missing" "$python_exe"
  exit 1
fi

if [[ ! -f "$train_scp" || ! -f "$cv_scp" ]]; then
  note "Missing dataset scp files under $dataset_root"
  write_results "failure" 1 "data" "unknown" "" "Expected train.scp/cv.scp missing; re-run prepare_assets.sh" "$python_exe"
  exit 1
fi

# NOTE: train/speech_enhancement uses a custom DistributedSampler that assumes
# len(dataset) >= world_size when shuffle=true. Our benchmark dataset is minimal
# (often 1 line), so DDP can fail with AssertionError. Create DDP-safe scp files
# with at least nproc entries by duplicating the first line.
ddp_train_scp="$stage_dir/train_ddp.scp"
ddp_cv_scp="$stage_dir/cv_ddp.scp"
first_train_line="$(head -n 1 "$train_scp" | tr -d '\r')"
first_cv_line="$(head -n 1 "$cv_scp" | tr -d '\r')"
if [[ -z "$first_train_line" || -z "$first_cv_line" ]]; then
  note "Empty train/cv scp files (train_scp=$train_scp, cv_scp=$cv_scp)"
  write_results "failure" 1 "data" "unknown" "" "train.scp/cv.scp empty; re-run prepare_assets.sh" "$python_exe"
  exit 1
fi
: >"$ddp_train_scp"
: >"$ddp_cv_scp"
for ((i=0; i<nproc; i++)); do
  printf '%s\n' "$first_train_line" >>"$ddp_train_scp"
  printf '%s\n' "$first_cv_line" >>"$ddp_cv_scp"
done
note "DDP-safe train_scp=$ddp_train_scp (nproc=$nproc)"
note "DDP-safe cv_scp=$ddp_cv_scp (nproc=$nproc)"
train_scp="$ddp_train_scp"
cv_scp="$ddp_cv_scp"

model_ckpt="$(
  PREP="$prepare_results" python - <<'PY' 2>>"$log_path" || true
import json, os, pathlib
p = pathlib.Path(os.environ["PREP"])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print((data.get("meta") or {}).get("model_checkpoint_path","") or "")
except Exception:
    print("")
PY
)"

if [[ -z "$model_ckpt" || ! -f "$model_ckpt" ]]; then
  note "Could not resolve model_checkpoint_path from prepare results (or file missing): $model_ckpt"
  write_results "failure" 1 "model" "unknown" "" "model_checkpoint_path missing/invalid in prepare results.json" "$python_exe"
  exit 1
fi

workdir="$repo_root/train/speech_enhancement"
config_path="$workdir/config/train/FRCRN_SE_16K.yaml"
if [[ ! -f "$config_path" ]]; then
  note "Missing config: $config_path"
  write_results "failure" 1 "entrypoint_not_found" "unknown" "" "Expected train config missing" "$python_exe"
  exit 1
fi

checkpoint_dir="$stage_dir/checkpoints"
mkdir -p "$checkpoint_dir"

master_port="29$(date +%S)"
decision_reason="Use torchrun (python -m torch.distributed.run) with train/speech_enhancement/train.py (FRCRN_SE_16K) for a minimal 1-batch DDP step; devices=${cuda_devices}, nproc=${nproc}."

runner_args=(
  python "$repo_root/benchmark_scripts/runner.py"
  --stage multi_gpu
  --task train
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --out-dir "$stage_dir"
  --workdir "$workdir"
  --assets-from "$prepare_results"
  --decision-reason "$decision_reason"
  --python "$python_exe"
  --env "CUDA_VISIBLE_DEVICES=${cuda_devices}"
  --env "OMP_NUM_THREADS=1"
)

cmd=(
  __PYTHON__
  -m torch.distributed.run
  --nproc_per_node "$nproc"
  --master_port "$master_port"
  "$repo_root/benchmark_scripts/torchrun_train_wrapper.py"
  --config "$config_path"
  --network "FRCRN_SE_16K"
  --tr-list "$train_scp"
  --cv-list "$cv_scp"
  --checkpoint_dir "$checkpoint_dir"
  --init_checkpoint_path "$model_ckpt"
  --use-cuda 1
  --batch_size 1
  --accu_grad 0
  --effec_batch_size 1
  --num_workers 0
  --max-epoch 0
  --print_freq 1
  --checkpoint_save_freq 1000000
)

note "Launching multi-GPU entrypoint via runner.py"
"${runner_args[@]}" -- "${cmd[@]}" >>"$log_path" 2>&1 || exit $?
