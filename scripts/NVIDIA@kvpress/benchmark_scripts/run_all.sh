#!/usr/bin/env bash
set -u -o pipefail

usage() {
  cat <<'EOF'
Run full benchmark workflow end-to-end (never aborts early).

Order:
  1) pyright
  2) prepare
  3) cpu
  4) cuda
  5) single_gpu
  6) multi_gpu
  7) env_size
  8) hallucination
  9) summary

Options:
  --report-path <path>   Sets $SCIMLOPSBENCH_REPORT for all stages
  --python <path>        Sets $SCIMLOPSBENCH_PYTHON for all stages
EOF
}

report_path=""
python_path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
cd "$repo_root"

if [[ -n "$report_path" ]]; then
  export SCIMLOPSBENCH_REPORT="$report_path"
fi
if [[ -n "$python_path" ]]; then
  export SCIMLOPSBENCH_PYTHON="$python_path"
fi

failed_stages=()

stage_outcome() {
  local stage="$1"
  local results="build_output/${stage}/results.json"
  if [[ ! -f "$results" ]]; then
    echo "failure"
    return 0
  fi
  python3 - <<PY 2>/dev/null || echo "failure"
import json
try:
  d = json.load(open("$results","r",encoding="utf-8"))
  status = d.get("status","failure")
  exit_code = int(d.get("exit_code", 1))
  if status == "skipped":
    print("skipped")
  elif status == "failure" or exit_code == 1:
    print("failure")
  else:
    print("success")
except Exception:
  print("failure")
PY
}

run_stage() {
  local stage="$1"; shift
  echo "=== Stage: $stage ==="
  "$@" || true
  local outcome
  outcome="$(stage_outcome "$stage")"
  echo "Stage $stage outcome: $outcome"
  if [[ "$outcome" == "failure" ]]; then
    failed_stages+=("$stage")
  fi
}

run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root"
run_stage prepare bash benchmark_scripts/prepare_assets.sh
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh
run_stage cuda python3 benchmark_scripts/check_cuda_available.py
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage env_size python3 benchmark_scripts/measure_env_size.py
run_stage hallucination python3 benchmark_scripts/validate_agent_report.py
run_stage summary python3 benchmark_scripts/summarize_results.py

echo "=== Final Summary ==="
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "Failed stages (in order): ${failed_stages[*]}"
  exit 1
fi
echo "All stages succeeded (skipped stages not counted as failures)."
exit 0

