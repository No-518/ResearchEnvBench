#!/usr/bin/env bash
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_BASE="build_output"

sys_py="$(command -v python3 || command -v python || true)"
if [[ -z "$sys_py" ]]; then
  echo "ERROR: python3/python not found on PATH (needed to read stage results.json)" >&2
  exit 1
fi

failed_stages=()

read_stage_outcome() {
  local stage="$1"
  local results_path="$REPO_ROOT/$OUT_BASE/$stage/results.json"
  if [[ ! -f "$results_path" ]]; then
    echo "missing_results"
    return 0
  fi
  "$sys_py" - <<PY 2>/dev/null || true
import json, sys
path = "$results_path"
try:
    d = json.load(open(path, "r", encoding="utf-8"))
    status = d.get("status", "failure")
    raw = d.get("exit_code", 1)
    try:
        exit_code = int(raw)
    except Exception:
        exit_code = 1
    print(f"{status}:{exit_code}")
except Exception:
    print("invalid_json:1")
PY
}

run_stage() {
  local stage="$1"; shift
  local cmd=("$@")

  echo "=== Stage: $stage ==="
  echo "Command: ${cmd[*]}"
  (cd "$REPO_ROOT" && "${cmd[@]}")
  local cmd_rc=$?
  echo "Stage command exit code: $cmd_rc (stage outcome determined from results.json)"

  local outcome
  outcome="$(read_stage_outcome "$stage")"
  if [[ "$outcome" == "missing_results" ]]; then
    echo "Stage $stage: FAILURE (missing results.json)"
    failed_stages+=("$stage")
    return 0
  fi

  local status="${outcome%%:*}"
  local ec="${outcome##*:}"

  if [[ "$status" == "skipped" ]]; then
    echo "Stage $stage: SKIPPED"
  elif [[ "$status" == "success" && "$ec" == "0" ]]; then
    echo "Stage $stage: SUCCESS"
  else
    echo "Stage $stage: FAILURE (status=$status exit_code=$ec)"
    failed_stages+=("$stage")
  fi
  return 0
}

set +e

run_stage "pyright" bash "$SCRIPT_DIR/run_pyright_missing_imports.sh"
run_stage "prepare" bash "$SCRIPT_DIR/prepare_assets.sh"
run_stage "cpu" bash "$SCRIPT_DIR/run_cpu_entrypoint.sh"
run_stage "cuda" "$sys_py" "$SCRIPT_DIR/check_cuda_available.py"
run_stage "single_gpu" bash "$SCRIPT_DIR/run_single_gpu_entrypoint.sh"
run_stage "multi_gpu" bash "$SCRIPT_DIR/run_multi_gpu_entrypoint.sh"
run_stage "env_size" "$sys_py" "$SCRIPT_DIR/measure_env_size.py"
run_stage "hallucination" "$sys_py" "$SCRIPT_DIR/validate_agent_report.py"
run_stage "summary" "$sys_py" "$SCRIPT_DIR/summarize_results.py"

set -e

echo "=== Final Summary ==="
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "Failed stages (in order): ${failed_stages[*]}"
  exit 1
fi
echo "All stages succeeded (skipped stages do not count as failures)."
exit 0
