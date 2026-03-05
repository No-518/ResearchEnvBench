#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run a minimal single-GPU eval using the repository entrypoint.

Outputs (always, even on failure):
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

Options:
  --python <path>        Override python executable (passed to runner.py)
  --report-path <path>   Override report.json path (passed to runner.py)
  --timeout-sec <int>    Default: 600
EOF
}

python_override=""
report_path=""
timeout_sec="600"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_override="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --timeout-sec)
      timeout_sec="${2:-}"; shift 2 ;;
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
STAGE_DIR="$ROOT/build_output/single_gpu"
MANIFEST_ENV="$ROOT/benchmark_assets/manifest.env"

mkdir -p "$STAGE_DIR" "$STAGE_DIR/tmp" "$STAGE_DIR/wandb"

if [[ ! -f "$MANIFEST_ENV" ]]; then
  mkdir -p "$STAGE_DIR"
  echo "[single_gpu] missing manifest: $MANIFEST_ENV (run prepare_assets.sh first)" >"$STAGE_DIR/log.txt"
  python3 - "$STAGE_DIR/results.json" <<'PY'
import json
from pathlib import Path

out = Path("build_output/single_gpu/results.json")
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "single_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 600,
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

export CUDA_VISIBLE_DEVICES="0"
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

decision_reason="Run AIMv1 official evaluation entrypoint on 1 GPU via torch distributed launcher; batch_size=1 and dataset contains 1 image to enforce 1 step."

runner_args=(
  --stage single_gpu
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
  --nproc-per-node=1
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
