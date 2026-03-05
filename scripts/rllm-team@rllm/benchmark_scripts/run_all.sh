#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run the full benchmark workflow end-to-end.

Order:
  pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary

Usage:
  bash benchmark_scripts/run_all.sh

Options:
  --report-path <path>   Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --python <path>        Explicit python to use for python stages (overrides report/env)
EOF
}

report_path=""
python_bin=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

report_path="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
export SCIMLOPSBENCH_REPORT="$report_path"

resolve_python_from_report() {
  python - <<'PY'
import json, os, pathlib, sys
rp = pathlib.Path(os.environ.get("SCIMLOPSBENCH_REPORT", "/opt/scimlopsbench/report.json"))
try:
    data = json.loads(rp.read_text(encoding="utf-8"))
except Exception as e:
    print("", end="")
    sys.exit(0)
py = data.get("python_path")
print(py if isinstance(py, str) else "", end="")
PY
}

if [[ -z "$python_bin" ]]; then
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    python_bin="$SCIMLOPSBENCH_PYTHON"
  else
    python_bin="$(resolve_python_from_report)"
  fi
fi

if [[ -n "$python_bin" ]]; then
  export SCIMLOPSBENCH_PYTHON="$python_bin"
fi

FAILED_STAGES=()

stage_failed() {
  local stage="$1"
  local res="build_output/$stage/results.json"
  if [[ ! -f "$res" ]]; then
    return 0
  fi
  local status exit_code
  local json_py="${python_bin:-python}"
  read -r status exit_code < <("$json_py" - <<PY 2>/dev/null || true
import json, pathlib
p = pathlib.Path("$res")
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("status","failure"), d.get("exit_code", 1))
except Exception:
    print("failure", 1)
PY
)
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    return 0
  fi
  return 1
}

run_stage() {
  local stage="$1"
  shift
  echo "==== stage: $stage ===="
  "$@" || true
  if stage_failed "$stage"; then
    FAILED_STAGES+=("$stage")
  fi
}

run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root" --report-path "$report_path" ${python_bin:+--python "$python_bin"}
run_stage prepare bash benchmark_scripts/prepare_assets.sh --report-path "$report_path" ${python_bin:+--python "$python_bin"}
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh --report-path "$report_path" ${python_bin:+--python "$python_bin"}
run_stage cuda "${python_bin:-python}" benchmark_scripts/check_cuda_available.py
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh --report-path "$report_path" ${python_bin:+--python "$python_bin"}
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh --report-path "$report_path" ${python_bin:+--python "$python_bin"}
run_stage env_size "${python_bin:-python}" benchmark_scripts/measure_env_size.py --report-path "$report_path"
run_stage hallucination "${python_bin:-python}" benchmark_scripts/validate_agent_report.py --report-path "$report_path"
run_stage summary "${python_bin:-python}" benchmark_scripts/summarize_results.py

echo "==== run_all complete ===="
if [[ ${#FAILED_STAGES[@]} -gt 0 ]]; then
  echo "Failed stages (in order): ${FAILED_STAGES[*]}"
  exit 1
fi
echo "All stages succeeded (skipped stages do not count as failures)."
exit 0
