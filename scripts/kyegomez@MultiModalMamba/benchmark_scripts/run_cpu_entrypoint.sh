#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step CPU inference via the repository's native entrypoint.

Outputs (written by runner.py):
  build_output/cpu/log.txt
  build_output/cpu/results.json

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

entrypoint=""
if [[ -f "example.py" ]]; then
  entrypoint="example.py"
elif [[ -f "model_example.py" ]]; then
  entrypoint="model_example.py"
fi

decision_reason="Using ${entrypoint:-<missing>} as the minimal repo entrypoint (one forward pass), forced to CPU via CUDA_VISIBLE_DEVICES=''."

runner_args=(
  --stage cpu
  --task infer
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --report-path "$report_path"
  --assets-from "build_output/prepare/results.json"
  --decision-reason "$decision_reason"
  --env "CUDA_VISIBLE_DEVICES="
  --env "SCIMLOPSBENCH_DATASET_DIR=$repo_root/benchmark_assets/dataset"
  --env "SCIMLOPSBENCH_MODEL_DIR=$repo_root/benchmark_assets/model"
)

if [[ -n "$python_bin" ]]; then
  runner_args+=(--python "$python_bin")
fi

python benchmark_scripts/runner.py "${runner_args[@]}" -- "{python}" "$entrypoint"

