#!/usr/bin/env bash
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

failures=()

read_stage_outcome() {
  local results_json="$1"
  if [[ ! -f "$results_json" ]]; then
    echo "failure"
    return 0
  fi
  python - <<'PY' "$results_json" 2>/dev/null || echo "failure"
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    status = str(data.get("status", "failure"))
    exit_code = int(data.get("exit_code", 1))
    if status == "skipped":
        print("skipped")
    elif status == "success" and exit_code == 0:
        print("success")
    else:
        print("failure")
except Exception:
    print("failure")
PY
}

run_stage() {
  local name="$1"
  shift
  echo
  echo "=============================="
  echo "Stage: $name"
  echo "=============================="
  set +e
  "$@"
  local cmd_ec=$?
  set -e

  local outcome
  outcome="$(read_stage_outcome "$REPO_ROOT/build_output/$name/results.json")"
  echo "Stage $name command_exit_code=$cmd_ec outcome=$outcome"

  if [[ "$outcome" == "failure" ]]; then
    failures+=("$name")
  fi
}

set -e

run_stage pyright bash "$REPO_ROOT/benchmark_scripts/run_pyright_missing_imports.sh" --repo "$REPO_ROOT"
run_stage prepare bash "$REPO_ROOT/benchmark_scripts/prepare_assets.sh"
run_stage cpu bash "$REPO_ROOT/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage cuda python "$REPO_ROOT/benchmark_scripts/check_cuda_available.py"
run_stage single_gpu bash "$REPO_ROOT/benchmark_scripts/run_single_gpu_entrypoint.sh"
run_stage multi_gpu bash "$REPO_ROOT/benchmark_scripts/run_multi_gpu_entrypoint.sh"
run_stage env_size python "$REPO_ROOT/benchmark_scripts/measure_env_size.py"
run_stage hallucination python "$REPO_ROOT/benchmark_scripts/validate_agent_report.py"
run_stage summary python "$REPO_ROOT/benchmark_scripts/summarize_results.py"

echo
echo "=============================="
if [[ ${#failures[@]} -gt 0 ]]; then
  echo "FAILED STAGES (in order): ${failures[*]}"
  exit 1
fi
echo "ALL STAGES PASSED"
exit 0
