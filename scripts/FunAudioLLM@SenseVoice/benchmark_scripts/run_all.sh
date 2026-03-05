#!/usr/bin/env bash
set -u -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

stages=(
  pyright
  prepare
  cpu
  cuda
  single_gpu
  multi_gpu
  env_size
  hallucination
  summary
)

failed_stages=()

run_stage() {
  local stage="$1"
  shift
  echo "[run_all] stage=${stage}"
  "$@" || true

  local results_path="build_output/${stage}/results.json"
  if [[ ! -f "$results_path" ]]; then
    echo "[run_all] stage=${stage} missing results.json (${results_path})"
    failed_stages+=("$stage")
    return 0
  fi

  local status exit_code
  read -r status exit_code < <(python - <<PY
import json
import pathlib
import sys

p = pathlib.Path("$results_path")
try:
    d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print("failure 1")
    raise SystemExit(0)

status = d.get("status", "failure")
exit_code = d.get("exit_code", 1)
print(f"{status} {exit_code}")
PY
)

  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    echo "[run_all] stage=${stage} => FAILURE"
    failed_stages+=("$stage")
  elif [[ "$status" == "skipped" ]]; then
    echo "[run_all] stage=${stage} => SKIPPED"
  else
    echo "[run_all] stage=${stage} => SUCCESS"
  fi
}

run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$REPO_ROOT"
run_stage prepare bash benchmark_scripts/prepare_assets.sh
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh
run_stage cuda python benchmark_scripts/check_cuda_available.py
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage env_size python benchmark_scripts/measure_env_size.py
run_stage hallucination python benchmark_scripts/validate_agent_report.py
run_stage summary python benchmark_scripts/summarize_results.py

if [[ "${#failed_stages[@]}" -gt 0 ]]; then
  echo "[run_all] FAILED stages (in order): ${failed_stages[*]}"
  exit 1
fi

echo "[run_all] all stages succeeded (skipped not counted as failure)"
exit 0

