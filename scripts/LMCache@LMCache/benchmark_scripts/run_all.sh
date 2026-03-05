#!/usr/bin/env bash
set -u -o pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

failed_stages=()

stage_outcome() {
  local stage="$1"
  local results_path="build_output/${stage}/results.json"

  if [[ ! -f "$results_path" ]]; then
    echo "[run_all] ${stage}: results.json missing (${results_path})"
    return 1
  fi

  local parsed
  if ! parsed="$(python3 - <<PY
import json, pathlib, sys
p = pathlib.Path("$results_path")
try:
  d = json.loads(p.read_text(encoding="utf-8"))
except Exception as e:
  print("invalid_json", file=sys.stdout)
  sys.exit(0)
status = d.get("status", "failure")
exit_code = d.get("exit_code", 1)
print(f"{status}\\n{exit_code}")
PY
  )"; then
    echo "[run_all] ${stage}: failed to parse results.json"
    return 1
  fi

  local status exit_code
  status="$(echo "$parsed" | sed -n '1p')"
  exit_code="$(echo "$parsed" | sed -n '2p')"

  if [[ "$status" == "invalid_json" ]]; then
    echo "[run_all] ${stage}: results.json invalid JSON"
    return 1
  fi

  if [[ "$status" == "skipped" ]]; then
    echo "[run_all] ${stage}: skipped"
    return 0
  fi

  if [[ "$status" == "failure" ]] || [[ "$exit_code" == "1" ]]; then
    echo "[run_all] ${stage}: failure (status=$status exit_code=$exit_code)"
    return 1
  fi

  echo "[run_all] ${stage}: success"
  return 0
}

run_stage() {
  local stage="$1"
  shift
  echo "================================================================================"
  echo "[run_all] Running stage: $stage"
  echo "================================================================================"

  # Never abort early; always continue to next stage.
  "$@"
  local cmd_rc=$?

  if ! stage_outcome "$stage"; then
    failed_stages+=("$stage")
  fi

  return $cmd_rc
}

run_stage pyright        bash benchmark_scripts/run_pyright_missing_imports.sh
run_stage prepare        bash benchmark_scripts/prepare_assets.sh
run_stage cpu            bash benchmark_scripts/run_cpu_entrypoint.sh
run_stage cuda           python3 benchmark_scripts/check_cuda_available.py
run_stage single_gpu     bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage multi_gpu      bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage env_size       python3 benchmark_scripts/measure_env_size.py
run_stage hallucination  python3 benchmark_scripts/validate_agent_report.py
run_stage summary        python3 benchmark_scripts/summarize_results.py

echo "================================================================================"
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "[run_all] FAILED stages (in order): ${failed_stages[*]}"
  exit 1
fi
echo "[run_all] All stages succeeded (or skipped)."
exit 0
