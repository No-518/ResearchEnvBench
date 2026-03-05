#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/benchmark_scripts"
BUILD_DIR="$REPO_ROOT/build_output"

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "ERROR: python/python3 not found in PATH" >&2
  exit 1
fi

failed_stages=()

read_stage_outcome() {
  local stage="$1"
  local results_json="$BUILD_DIR/$stage/results.json"
  "$PYTHON_BIN" - <<PY
import json, pathlib, sys
p = pathlib.Path(${results_json@Q})
if not p.exists():
  print("failure 1 missing_stage_results")
  sys.exit(0)
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print("failure 1 invalid_json")
  sys.exit(0)
status = str(data.get("status","failure"))
exit_code = int(data.get("exit_code", 1) or 1)
failure_category = str(data.get("failure_category",""))
print(status, exit_code, failure_category)
PY
}

run_stage() {
  local stage="$1"
  shift
  echo "==> Stage: $stage"
  "$@" >/dev/null 2>&1
  local script_rc=$?

  local outcome
  outcome="$(read_stage_outcome "$stage")"
  local status exit_code failure_category
  status="$(awk '{print $1}' <<<"$outcome")"
  exit_code="$(awk '{print $2}' <<<"$outcome")"
  failure_category="$(awk '{print $3}' <<<"$outcome")"

  echo "    status=$status exit_code=$exit_code script_rc=$script_rc failure_category=${failure_category:-}"
  if [[ "$status" == "failure" || "$exit_code" -eq 1 ]]; then
    failed_stages+=("$stage")
  fi
}

run_stage "pyright" bash "$SCRIPTS_DIR/run_pyright_missing_imports.sh" --repo "$REPO_ROOT"
run_stage "prepare" bash "$SCRIPTS_DIR/prepare_assets.sh"
run_stage "cpu" bash "$SCRIPTS_DIR/run_cpu_entrypoint.sh"
run_stage "cuda" "$PYTHON_BIN" "$SCRIPTS_DIR/check_cuda_available.py"
run_stage "single_gpu" bash "$SCRIPTS_DIR/run_single_gpu_entrypoint.sh"
run_stage "multi_gpu" bash "$SCRIPTS_DIR/run_multi_gpu_entrypoint.sh"
run_stage "env_size" "$PYTHON_BIN" "$SCRIPTS_DIR/measure_env_size.py"
run_stage "hallucination" "$PYTHON_BIN" "$SCRIPTS_DIR/validate_agent_report.py"
run_stage "summary" "$PYTHON_BIN" "$SCRIPTS_DIR/summarize_results.py"

if [[ "${#failed_stages[@]}" -gt 0 ]]; then
  echo ""
  echo "FAILED STAGES (in order): ${failed_stages[*]}"
  exit 1
fi

echo ""
echo "All stages succeeded."
exit 0
