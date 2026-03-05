#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYBIN="$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)"

failures=()

stage_result() {
  local stage="$1"
  local results_json="$REPO_ROOT/build_output/$stage/results.json"
  if [[ ! -f "$results_json" ]]; then
    echo "[run_all] $stage: results.json missing -> FAILED"
    failures+=("$stage")
    return 0
  fi

  local parsed
  parsed="$("$PYBIN" - "$results_json" <<'PY' 2>/dev/null || true
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p, "r", encoding="utf-8"))
except Exception:
    print("invalid_json 1")
    raise SystemExit(0)
status = d.get("status", "failure")
exit_code = d.get("exit_code", 1)
print(f"{status} {exit_code}")
PY
)"

  local status exit_code
  status="$(echo "$parsed" | awk '{print $1}')"
  exit_code="$(echo "$parsed" | awk '{print $2}')"

  if [[ "$status" == "skipped" ]]; then
    echo "[run_all] $stage: skipped"
    return 0
  fi
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    echo "[run_all] $stage: FAILED (status=$status exit_code=$exit_code)"
    failures+=("$stage")
  else
    echo "[run_all] $stage: success"
  fi
  return 0
}

run_stage() {
  local stage="$1"; shift
  echo "============================================================"
  echo "[run_all] stage=$stage cmd=$*"
  echo "============================================================"
  "$@"
  local rc=$?
  echo "[run_all] stage=$stage finished rc=$rc"
  stage_result "$stage"
}

main() {
  # 1) pyright
  run_stage "pyright" bash "$REPO_ROOT/benchmark_scripts/run_pyright_missing_imports.sh" --repo "$REPO_ROOT"

  # 2) prepare
  run_stage "prepare" bash "$REPO_ROOT/benchmark_scripts/prepare_assets.sh"

  # 3) cpu
  run_stage "cpu" bash "$REPO_ROOT/benchmark_scripts/run_cpu_entrypoint.sh"

  # 4) cuda
  run_stage "cuda" "$PYBIN" "$REPO_ROOT/benchmark_scripts/check_cuda_available.py"

  # 5) single gpu
  run_stage "single_gpu" bash "$REPO_ROOT/benchmark_scripts/run_single_gpu_entrypoint.sh"

  # 6) multi gpu
  run_stage "multi_gpu" bash "$REPO_ROOT/benchmark_scripts/run_multi_gpu_entrypoint.sh"

  # 7) env size
  run_stage "env_size" "$PYBIN" "$REPO_ROOT/benchmark_scripts/measure_env_size.py"

  # 8) hallucination validation
  run_stage "hallucination" "$PYBIN" "$REPO_ROOT/benchmark_scripts/validate_agent_report.py"

  # 9) summary
  run_stage "summary" "$PYBIN" "$REPO_ROOT/benchmark_scripts/summarize_results.py"

  echo "============================================================"
  if [[ "${#failures[@]}" -gt 0 ]]; then
    echo "[run_all] FAILED stages (execution order): ${failures[*]}"
    exit 1
  fi
  echo "[run_all] All stages succeeded."
  exit 0
}

main "$@"
