#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step single-GPU inference via the repository's native entrypoint.

Outputs:
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

Behavior:
  - Forces CUDA_VISIBLE_DEVICES=0

Optional:
  --repo <path>            Repository root (default: auto-detect)
  --report-path <path>     Agent report path (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  --python <path>          Explicit python executable (highest priority)
  --timeout-sec <n>        Default: 600
EOF
}

repo_root=""
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_bin=""
timeout_sec="600"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo_root="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$repo_root" ]]; then
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

cd "$repo_root"

json_py="$(command -v python3 || command -v python || true)"
if [[ -z "$json_py" ]]; then
  echo "python not found on PATH; cannot parse cuda results." >&2
  exit 1
fi

cuda_available="unknown"
if [[ -f "build_output/cuda/results.json" ]]; then
  cuda_available="$("$json_py" - <<'PY' || true
import json
from pathlib import Path
p = Path("build_output/cuda/results.json")
try:
  d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print("unknown")
  raise SystemExit(0)
obs = d.get("observed") if isinstance(d, dict) else None
val = None
if isinstance(obs, dict) and isinstance(obs.get("cuda_available"), bool):
  val = obs["cuda_available"]
elif isinstance(d.get("cuda_available"), bool):
  val = d["cuda_available"]
elif d.get("exit_code") == 0:
  val = True
elif d.get("exit_code") == 1:
  val = False
print("true" if val is True else "false" if val is False else "unknown")
PY
)"
fi

entrypoint=""
if [[ -f "example.py" ]]; then
  entrypoint="example.py"
elif [[ -f "model_example.py" ]]; then
  entrypoint="model_example.py"
fi

runner_args=(
  --stage single_gpu
  --task infer
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --report-path "$report_path"
  --assets-from "build_output/prepare/results.json"
  --env "CUDA_VISIBLE_DEVICES=0"
  --env "SCIMLOPSBENCH_DATASET_DIR=$repo_root/benchmark_assets/dataset"
  --env "SCIMLOPSBENCH_MODEL_DIR=$repo_root/benchmark_assets/model"
)
if [[ -n "$python_bin" ]]; then
  runner_args+=(--python "$python_bin")
fi

if [[ -z "$entrypoint" ]]; then
  python benchmark_scripts/runner.py "${runner_args[@]}" \
    --failure-category entrypoint_not_found \
    --decision-reason "No supported repo entrypoint found (expected example.py or model_example.py)." \
    -- bash -lc 'echo "entrypoint_not_found: expected example.py or model_example.py" >&2; exit 2'
  exit 1
fi

decision_reason="Running ${entrypoint} as repo entrypoint on a single GPU via entrypoint_wrapper.py (CUDA_VISIBLE_DEVICES=0)."

python benchmark_scripts/runner.py "${runner_args[@]}" \
  --decision-reason "$decision_reason" \
  -- "{python}" benchmark_scripts/entrypoint_wrapper.py --entrypoint "$entrypoint" --device cuda --local-rank 0
