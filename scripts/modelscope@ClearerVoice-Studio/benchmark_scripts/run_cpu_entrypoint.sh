#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step CPU training via the repository's native entrypoint.

Entrypoint (repo):
  train/speech_enhancement/train.py (FRCRN_SE_16K)

Requires:
  build_output/prepare/results.json (from benchmark_scripts/prepare_assets.sh)

Outputs:
  build_output/cpu/log.txt
  build_output/cpu/results.json

Options:
  --python <path>        Override python for the entrypoint (otherwise uses report.json via runner.py)
  --timeout-sec <sec>    Default: 600
EOF
}

python_override=""
timeout_sec="600"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/cpu"
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

note() { echo "[cpu] $*" >>"$log_path"; }

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
    "stage": "cpu",
    "task": "train",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 600,
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

on_unhandled_error() {
  local ec=$?
  trap - ERR
  set +e
  write_results "failure" 1 "runtime" "unknown" "" "Unhandled error" "${python_override:-}" || true
  exit "$ec"
}
trap on_unhandled_error ERR

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_override="${2:-}"; shift 2 ;;
    --timeout-sec)
      timeout_sec="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      note "Unknown argument: $1"
      write_results "failure" 1 "args_unknown" "unknown" "$0 $*" "Unknown CLI argument" "${python_override:-}"
      exit 1 ;;
  esac
done

prepare_results="$repo_root/build_output/prepare/results.json"
dataset_root="$repo_root/benchmark_assets/dataset"
train_scp="$dataset_root/train.scp"
cv_scp="$dataset_root/cv.scp"

if [[ ! -f "$prepare_results" ]]; then
  note "Missing prepare results: $prepare_results"
  write_results "failure" 1 "data" "unknown" "" "prepare stage results.json missing" ""
  exit 1
fi

if [[ ! -f "$train_scp" || ! -f "$cv_scp" ]]; then
  note "Missing dataset scp files under $dataset_root"
  write_results "failure" 1 "data" "unknown" "" "Expected train.scp/cv.scp missing; re-run prepare_assets.sh" ""
  exit 1
fi

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
  write_results "failure" 1 "model" "unknown" "" "model_checkpoint_path missing/invalid in prepare results.json" ""
  exit 1
fi

workdir="$repo_root/train/speech_enhancement"
config_path="$workdir/config/train/FRCRN_SE_16K.yaml"
if [[ ! -f "$config_path" ]]; then
  note "Missing config: $config_path"
  write_results "failure" 1 "entrypoint_not_found" "unknown" "" "Expected train config missing" ""
  exit 1
fi

checkpoint_dir="$stage_dir/checkpoints"
mkdir -p "$checkpoint_dir"

decision_reason="Use train/speech_enhancement/train.py (FRCRN_SE_16K) for a 1-batch training step on a 1-sample dataset; force CPU via --use-cuda 0 and CUDA_VISIBLE_DEVICES=."

runner_args=(
  python "$repo_root/benchmark_scripts/runner.py"
  --stage cpu
  --task train
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --out-dir "$stage_dir"
  --workdir "$workdir"
  --assets-from "$prepare_results"
  --decision-reason "$decision_reason"
  --env "CUDA_VISIBLE_DEVICES="
  --env "OMP_NUM_THREADS=1"
)
if [[ -n "$python_override" ]]; then
  runner_args+=(--python "$python_override")
fi

cmd=(
  __PYTHON__
  train.py
  --config "$config_path"
  --network "FRCRN_SE_16K"
  --tr-list "$train_scp"
  --cv-list "$cv_scp"
  --checkpoint_dir "$checkpoint_dir"
  --init_checkpoint_path "$model_ckpt"
  --use-cuda 0
  --batch_size 1
  --accu_grad 0
  --effec_batch_size 1
  --num_workers 0
  --max-epoch 0
  --print_freq 1
  --checkpoint_save_freq 1000000
)

note "Launching CPU entrypoint via runner.py"
"${runner_args[@]}" -- "${cmd[@]}" >>"$log_path" 2>&1 || exit $?
