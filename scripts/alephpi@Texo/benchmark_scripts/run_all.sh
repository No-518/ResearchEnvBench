#!/usr/bin/env bash
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

REPORT_PATH="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"

PY_JSON="$(command -v python3 || true)"
if [[ -z "$PY_JSON" ]]; then
  PY_JSON="$(command -v python || true)"
fi
if [[ -z "$PY_JSON" ]]; then
  PY_JSON="python"
fi

resolve_report_python() {
  if [[ -z "$PY_JSON" ]]; then
    echo ""
    return 0
  fi
  "$PY_JSON" - <<PY 2>/dev/null || true
import json, pathlib
rp = pathlib.Path("${REPORT_PATH}")
try:
    data = json.loads(rp.read_text(encoding="utf-8"))
    print(data.get("python_path","") or "")
except Exception:
    print("")
PY
}

PYTHON_BIN="${SCIMLOPSBENCH_PYTHON:-$(resolve_report_python)}"
if [[ -z "$PYTHON_BIN" ]]; then
  # Stages that require python will record missing_report themselves.
  PYTHON_BIN="${PY_JSON:-python}"
fi

failures=()

read_stage_outcome() {
  local stage="$1"
  local res="build_output/${stage}/results.json"
  if [[ ! -f "$res" ]]; then
    echo "failure"
    return 0
  fi
  "$PY_JSON" - <<PY 2>/dev/null || echo "failure"
import json
try:
    data = json.load(open("${res}"))
    status = str(data.get("status","failure"))
    raw_exit = data.get("exit_code", 1)
    try:
        exit_code = int(raw_exit)
    except Exception:
        exit_code = 1
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
  echo "===== stage: ${stage} ====="
  "$@" || true
  outcome="$(read_stage_outcome "$stage")"
  echo "===== stage: ${stage} outcome: ${outcome} ====="
  if [[ "$outcome" == "failure" ]]; then
    failures+=("$stage")
  fi
}

run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --python "$PYTHON_BIN"
run_stage prepare bash benchmark_scripts/prepare_assets.sh --python "$PYTHON_BIN"
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh
run_stage cuda "$PYTHON_BIN" benchmark_scripts/check_cuda_available.py
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage env_size "$PY_JSON" benchmark_scripts/measure_env_size.py
run_stage hallucination "$PY_JSON" benchmark_scripts/validate_agent_report.py
run_stage summary "$PY_JSON" benchmark_scripts/summarize_results.py

echo "===== run_all complete ====="
if [[ "${#failures[@]}" -gt 0 ]]; then
  echo "Failed stages (in order): ${failures[*]}"
  exit 1
fi
echo "All stages succeeded (skipped not counted as failure)."
exit 0
