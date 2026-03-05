#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run the full benchmark pipeline end-to-end (never aborts early).

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

Optional:
  --report-path <path>   Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --python <path>        Override python interpreter for stages (sets $SCIMLOPSBENCH_PYTHON)
EOF
}

report_path=""
python_override=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --python)
      python_override="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2
      ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

sys_py="$(command -v python3 || command -v python || true)"
if [[ -z "$sys_py" ]]; then
  echo "python/python3 not found on PATH; cannot orchestrate." >&2
  exit 1
fi

if [[ -n "$report_path" ]]; then
  export SCIMLOPSBENCH_REPORT="$report_path"
fi
if [[ -n "$python_override" ]]; then
  export SCIMLOPSBENCH_PYTHON="$python_override"
fi

bench_py="${SCIMLOPSBENCH_PYTHON:-}"
if [[ -z "$bench_py" ]]; then
  bench_py="$("$sys_py" benchmark_scripts/runner.py --stage _ --task _ --print-python 2>/dev/null || true)"
fi
bench_py="${bench_py:-$sys_py}"

failures=()

json_field() {
  local file="$1"
  local key="$2"
  "$sys_py" - "$file" "$key" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
try:
  data = json.load(open(path, "r", encoding="utf-8"))
except Exception:
  print("")
  raise SystemExit(0)
v = data.get(key, "")
if isinstance(v, (dict, list)):
  print(json.dumps(v))
else:
  print(v)
PY
}

record_stage_outcome() {
  local stage="$1"
  local res="build_output/$stage/results.json"
  if [[ ! -f "$res" ]]; then
    failures+=("$stage")
    echo "[$stage] results.json missing -> failure"
    return
  fi
  local st ec
  st="$(json_field "$res" status)"
  ec="$(json_field "$res" exit_code)"
  if [[ "$st" == "skipped" ]]; then
    echo "[$stage] skipped"
    return
  fi
  if [[ "$st" == "failure" || "$ec" == "1" ]]; then
    failures+=("$stage")
    echo "[$stage] failure"
    return
  fi
  echo "[$stage] success"
}

run_stage() {
  local stage="$1"; shift
  echo ""
  echo "== Stage: $stage =="
  set +e
  "$@"
  local rc="$?"
  set -e
  echo "[$stage] script exit code: $rc"
  record_stage_outcome "$stage"
}

set -e

run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh
run_stage prepare bash benchmark_scripts/prepare_assets.sh
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh
run_stage cuda "$bench_py" benchmark_scripts/check_cuda_available.py
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage env_size "$bench_py" benchmark_scripts/measure_env_size.py
run_stage hallucination "$bench_py" benchmark_scripts/validate_agent_report.py
run_stage summary "$bench_py" benchmark_scripts/summarize_results.py

echo ""
if [[ "${#failures[@]}" -gt 0 ]]; then
  echo "FAILED STAGES (in order): ${failures[*]}"
  exit 1
fi
echo "All stages succeeded (skipped stages excluded)."
exit 0

