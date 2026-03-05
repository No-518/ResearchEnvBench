#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run the full benchmark workflow in order. Does NOT abort early on failures.

Optional:
  --python <path>        Export SCIMLOPSBENCH_PYTHON for all stages
  --report-path <path>   Export SCIMLOPSBENCH_REPORT for all stages
EOF
}

python_bin=""
report_path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

if [[ -n "$python_bin" ]]; then
  export SCIMLOPSBENCH_PYTHON="$python_bin"
fi
if [[ -n "$report_path" ]]; then
  export SCIMLOPSBENCH_REPORT="$report_path"
fi

failed_stages=()

json_status_exit() {
  local stage="$1"
  local res="$repo_root/build_output/$stage/results.json"
  if [[ ! -f "$res" ]]; then
    echo "failure 1"
    return
  fi
  python3 - "$res" <<'PY'
import json, sys
p = sys.argv[1]
try:
    d = json.loads(open(p, "r", encoding="utf-8").read())
    s = d.get("status", "failure")
    ec_raw = d.get("exit_code", 1)
    try:
        ec = int(ec_raw)
    except Exception:
        ec = 1
    print(s, ec)
except Exception:
    print("failure 1")
PY
}

run_stage() {
  local stage="$1"; shift
  echo "=== $stage ==="
  "$@" || true
  read -r st ec < <(json_status_exit "$stage")
  if [[ "$st" == "failure" || "$ec" != "0" ]]; then
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

echo "=== done ==="
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "failed stages (in order): ${failed_stages[*]}"
  exit 1
fi
echo "all stages succeeded (or skipped)"
exit 0
