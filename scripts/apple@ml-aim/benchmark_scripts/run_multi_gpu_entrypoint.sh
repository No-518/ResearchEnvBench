#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU (DDP) eval using the repository entrypoint.

Outputs (always, even on failure):
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Options:
  --python <path>              Override python executable (passed to runner.py and GPU detection)
  --report-path <path>         Override report.json path (for python resolution)
  --timeout-sec <int>          Default: 1200
  --cuda-visible-devices <s>   Default: 0,1
  --nproc-per-node <int>       Default: inferred from CUDA_VISIBLE_DEVICES count
EOF
}

python_override=""
report_path=""
timeout_sec="1200"
cuda_visible_devices="0,1"
nproc_per_node=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_override="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --timeout-sec)
      timeout_sec="${2:-}"; shift 2 ;;
    --cuda-visible-devices)
      cuda_visible_devices="${2:-}"; shift 2 ;;
    --nproc-per-node)
      nproc_per_node="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_DIR="$ROOT/build_output/multi_gpu"
LOG_TXT="$STAGE_DIR/log.txt"
RESULTS_JSON="$STAGE_DIR/results.json"
MANIFEST_ENV="$ROOT/benchmark_assets/manifest.env"

mkdir -p "$STAGE_DIR" "$STAGE_DIR/tmp" "$STAGE_DIR/wandb"

git_commit="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"

if [[ ! -f "$MANIFEST_ENV" ]]; then
  echo "[multi_gpu] missing manifest: $MANIFEST_ENV (run prepare_assets.sh first)" >"$LOG_TXT"
  python3 - "$RESULTS_JSON" <<'PY'
import json
from pathlib import Path

out = Path("build_output/multi_gpu/results.json")
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "prepare_assets.sh did not run (manifest missing)",
  },
  "failure_category": "data",
  "error_excerpt": "Missing benchmark_assets/manifest.env (run prepare_assets.sh first).",
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
fi

# shellcheck disable=SC1090
source "$MANIFEST_ENV"

export CUDA_VISIBLE_DEVICES="$cuda_visible_devices"
export HF_HOME="$ROOT/benchmark_assets/cache/huggingface"
export TRANSFORMERS_CACHE="$ROOT/benchmark_assets/cache/huggingface/transformers"
export TORCH_HOME="$ROOT/benchmark_assets/cache/torch"
export XDG_CACHE_HOME="$ROOT/benchmark_assets/cache/xdg"
export WANDB_MODE="offline"
export WANDB_DIR="$STAGE_DIR/wandb"
export TMPDIR="$STAGE_DIR/tmp"
export OMP_NUM_THREADS="1"
export SCIMLOPSBENCH_AIM_ATTNPROBE_COMPAT="1"
export PYTHONPATH="$ROOT/benchmark_scripts${PYTHONPATH:+:$PYTHONPATH}"

count_visible_gpus() {
  python3 - "$report_path" "$python_override" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

report_path = sys.argv[1] if sys.argv[1] else os.environ.get("SCIMLOPSBENCH_REPORT", "/opt/scimlopsbench/report.json")
python_override = sys.argv[2] if sys.argv[2] else os.environ.get("SCIMLOPSBENCH_PYTHON", "")

py = None
if python_override:
    py = python_override
else:
    p = Path(report_path)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        py = data.get("python_path")
if not py:
    py = "python"

try:
    out = subprocess.check_output(
        [py, "-c", "import torch; print(torch.cuda.device_count())"],
        text=True,
        stderr=subprocess.STDOUT,
        timeout=20,
        env={**os.environ, "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "")},
    ).strip()
    print(int(out))
except Exception:
    print(-1)
PY
}

gpu_count="$(count_visible_gpus)"
{
  echo "[multi_gpu] CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
  echo "[multi_gpu] detected gpu_count=$gpu_count"
} >"$LOG_TXT"

if [[ "$gpu_count" -lt 2 ]]; then
  echo "[multi_gpu] need >=2 GPUs for this stage; failing" >>"$LOG_TXT"
  python3 - "$RESULTS_JSON" "$git_commit" "$gpu_count" <<PY
import json
import sys
from pathlib import Path

out = Path("$RESULTS_JSON")
git_commit = sys.argv[1]
gpu_count = int(sys.argv[2])
payload = {
  "status": "failure",
  "skip_reason": "insufficient_hardware",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "python -m torch.distributed.run --standalone --nnodes=1 --nproc-per-node=2 aim-v1/main_attnprobe.py ...",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "$DATASET_ROOT_ABS", "source": "", "version": "", "sha256": ""},
    "model": {"path": "$MODEL_ROOT_ABS", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": git_commit,
    "env_vars": {"CUDA_VISIBLE_DEVICES": "$CUDA_VISIBLE_DEVICES"},
    "decision_reason": f"Detected gpu_count={gpu_count}; requires >=2 for multi-GPU stage.",
  },
  "failure_category": "runtime",
  "error_excerpt": "Insufficient GPUs (need >=2).",
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY
  exit 1
fi

if [[ -z "$nproc_per_node" ]]; then
  # Infer from CUDA_VISIBLE_DEVICES list length.
  IFS=',' read -r -a _gpus <<<"$CUDA_VISIBLE_DEVICES"
  nproc_per_node="${#_gpus[@]}"
fi

decision_reason="Run AIMv1 official evaluation entrypoint on >=2 GPUs via torch distributed launcher; batch_size=1 and dataset contains 1 image to enforce 1 step."

runner_args=(
  --stage multi_gpu
  --task infer
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --out-dir "$STAGE_DIR"
  --assets-from "$ROOT/build_output/prepare/results.json"
  --decision-reason "$decision_reason"
)

if [[ -n "$report_path" ]]; then
  runner_args+=(--report-path "$report_path")
fi
if [[ -n "$python_override" ]]; then
  runner_args+=(--python "$python_override")
fi

cmd=(
  python -m torch.distributed.run
  --standalone
  --nnodes=1
  --nproc-per-node="$nproc_per_node"
  aim-v1/main_attnprobe.py
  --model "$MODEL_NAME"
  --batch-size 1
  --num_workers 0
  --data-path "$DATASET_ROOT_ABS"
  --probe-layers "$PROBE_LAYERS"
  --backbone-ckpt-path "$BACKBONE_CKPT_ABS"
  --head-ckpt-path "$HEAD_CKPT_ABS"
)

python3 "$ROOT/benchmark_scripts/runner.py" "${runner_args[@]}" -- "${cmd[@]}"
exit $?
