#!/usr/bin/env bash
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

REPORT_PATH="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
export SCIMLOPSBENCH_REPORT="$REPORT_PATH"

pick_system_python() {
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
  elif command -v python >/dev/null 2>&1; then
    echo "python"
  else
    echo ""
  fi
}

SYS_PY="$(pick_system_python)"

resolve_python_from_report() {
  if [[ -z "$SYS_PY" ]]; then
    echo ""
    return 1
  fi
  if [[ ! -f "$REPORT_PATH" ]]; then
    echo ""
    return 1
  fi
  "$SYS_PY" - <<'PY' 2>/dev/null || true
import json, os, sys
path = os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"
try:
    data = json.load(open(path, "r", encoding="utf-8"))
except Exception:
    sys.exit(1)
pp = data.get("python_path")
if isinstance(pp, str) and pp.strip():
    print(pp.strip())
PY
}

PYTHON_BIN="$(resolve_python_from_report)"
if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$SYS_PY"
fi

if [[ -n "$PYTHON_BIN" ]]; then
  export SCIMLOPSBENCH_PYTHON="$PYTHON_BIN"
fi

echo "Repo root: $REPO_ROOT"
echo "Report path: $REPORT_PATH"
echo "Python for stages: ${SCIMLOPSBENCH_PYTHON:-<unset>}"

FAILED_STAGES=()

read_stage_outcome() {
  local stage="$1"
  local results_path="$REPO_ROOT/build_output/$stage/results.json"
  if [[ -z "$SYS_PY" ]]; then
    echo "failure 1"
    return
  fi
  if [[ ! -f "$results_path" ]]; then
    echo "failure 1"
    return
  fi
  "$SYS_PY" - <<PY 2>/dev/null || true
import json, sys
try:
    data = json.load(open("$results_path","r",encoding="utf-8"))
except Exception:
    print("failure 1")
    raise SystemExit(0)
status = data.get("status","failure")
exit_code = int(data.get("exit_code", 1))
print(f"{status} {exit_code}")
PY
}

run_stage() {
  local stage="$1"; shift
  echo ""
  echo "===== STAGE: $stage ====="
  "$@" || true
  local status exit_code
  read -r status exit_code < <(read_stage_outcome "$stage")
  echo "Stage $stage outcome: status=$status exit_code=$exit_code"
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    FAILED_STAGES+=("$stage")
  fi
}

cd "$REPO_ROOT"

PY_STAGE_ARGS=()
if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  PY_STAGE_ARGS=(--python "$SCIMLOPSBENCH_PYTHON")
fi

run_stage "pyright" bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$REPO_ROOT" "${PY_STAGE_ARGS[@]}"
run_stage "prepare" bash benchmark_scripts/prepare_assets.sh --repo "$REPO_ROOT" "${PY_STAGE_ARGS[@]}"
run_stage "cpu" bash benchmark_scripts/run_cpu_entrypoint.sh --repo "$REPO_ROOT" "${PY_STAGE_ARGS[@]}"

if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  run_stage "cuda" "$SCIMLOPSBENCH_PYTHON" benchmark_scripts/check_cuda_available.py
  run_stage "single_gpu" bash benchmark_scripts/run_single_gpu_entrypoint.sh --repo "$REPO_ROOT" --python "$SCIMLOPSBENCH_PYTHON"
  run_stage "multi_gpu" bash benchmark_scripts/run_multi_gpu_entrypoint.sh --repo "$REPO_ROOT" --python "$SCIMLOPSBENCH_PYTHON"
  run_stage "env_size" "$SCIMLOPSBENCH_PYTHON" benchmark_scripts/measure_env_size.py --report-path "$REPORT_PATH"
  run_stage "hallucination" "$SCIMLOPSBENCH_PYTHON" benchmark_scripts/validate_agent_report.py --report-path "$REPORT_PATH"
  run_stage "summary" "$SCIMLOPSBENCH_PYTHON" benchmark_scripts/summarize_results.py
else
  echo "No Python available to run python stages; marking remaining stages as failed due to missing results."
  # Best effort: run summary with system python if available.
  if [[ -n "$SYS_PY" ]]; then
    run_stage "cuda" "$SYS_PY" benchmark_scripts/check_cuda_available.py
    run_stage "env_size" "$SYS_PY" benchmark_scripts/measure_env_size.py --report-path "$REPORT_PATH"
    run_stage "hallucination" "$SYS_PY" benchmark_scripts/validate_agent_report.py --report-path "$REPORT_PATH"
    run_stage "summary" "$SYS_PY" benchmark_scripts/summarize_results.py
  fi
fi

echo ""
if [[ ${#FAILED_STAGES[@]} -gt 0 ]]; then
  echo "FAILED STAGES (in order): ${FAILED_STAGES[*]}"
  exit 1
fi
echo "All stages succeeded (skipped stages are not failures)."
exit 0
