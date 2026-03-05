#!/usr/bin/env bash
set -u

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Run the full benchmark workflow (does not abort on stage failures).

Optional:
  --report-path <path>   Exports SCIMLOPSBENCH_REPORT for all stages.
  --python <path>        Exports SCIMLOPSBENCH_PYTHON for all stages.

Outputs:
  build_output/<stage>/{log.txt,results.json} for each stage
  build_output/summary/results.json (aggregated)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) export SCIMLOPSBENCH_REPORT="${2:-}"; shift 2 ;;
    --python) export SCIMLOPSBENCH_PYTHON="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

failed_stages=()

stage_outcome() {
  local stage="$1"
  local results_path="build_output/${stage}/results.json"

  if [[ ! -f "$results_path" ]]; then
    echo "failure 1 missing_stage_results"
    return 0
  fi

  python3 - "$results_path" <<'PY'
import json, sys
from pathlib import Path

p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print("failure 1 invalid_json")
    raise SystemExit(0)

status = data.get("status", "failure")
exit_code = data.get("exit_code", 1)
failure_category = data.get("failure_category", "unknown")
try:
    exit_code = int(exit_code)
except Exception:
    exit_code = 1

print(f"{status} {exit_code} {failure_category}")
PY
}

run_stage() {
  local stage="$1"
  shift
  echo "========== Stage: ${stage} =========="

  # Run stage command; never abort early.
  set +e
  "$@"
  local rc=$?
  set -e

  local status exit_code failure_category
  read -r status exit_code failure_category < <(stage_outcome "$stage")

  echo "[run_all] stage=${stage} script_rc=${rc} status=${status} exit_code=${exit_code} failure_category=${failure_category}"

  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    failed_stages+=("$stage")
  fi
}

set -e

run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh
run_stage prepare bash benchmark_scripts/prepare_assets.sh
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh
run_stage cuda python3 benchmark_scripts/check_cuda_available.py
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage env_size python3 benchmark_scripts/measure_env_size.py
run_stage hallucination python3 benchmark_scripts/validate_agent_report.py
run_stage summary python3 benchmark_scripts/summarize_results.py

if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "========== Final: FAILED =========="
  printf '%s\n' "${failed_stages[@]}" | awk '{print "- " $0}'
  exit 1
fi

echo "========== Final: SUCCESS =========="
exit 0

