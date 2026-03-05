#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

failed_stages=()

stage_outcome() {
  local stage="$1"
  local results_path="$repo_root/build_output/$stage/results.json"
  if [[ ! -f "$results_path" ]]; then
    echo "missing 1"
    return 0
  fi
  python3 - <<PY 2>/dev/null || echo "invalid 1"
import json, pathlib
p=pathlib.Path(${results_path@Q})
try:
  d=json.loads(p.read_text(encoding="utf-8"))
  status=str(d.get("status","failure"))
  exit_code=int(d.get("exit_code",1))
  print(status, exit_code)
except Exception:
  print("invalid", 1)
PY
}

run_stage() {
  local stage="$1"; shift
  echo ""
  echo "===== Stage: $stage ====="
  set +e
  "$@"
  local rc=$?
  set -e

  read -r status exit_code < <(stage_outcome "$stage")
  echo "[run_all] $stage script_rc=$rc results_status=$status results_exit_code=$exit_code"

  if [[ "$status" == "failure" || "$exit_code" -eq 1 || "$status" == "missing" || "$status" == "invalid" ]]; then
    failed_stages+=("$stage")
  fi
}

set -e
cd "$repo_root"

run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root"
run_stage prepare bash benchmark_scripts/prepare_assets.sh
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh
run_stage cuda python3 benchmark_scripts/check_cuda_available.py
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage env_size python3 benchmark_scripts/measure_env_size.py
run_stage hallucination python3 benchmark_scripts/validate_agent_report.py
run_stage summary python3 benchmark_scripts/summarize_results.py

echo ""
if [[ "${#failed_stages[@]}" -gt 0 ]]; then
  echo "[run_all] Failed stages (in order): ${failed_stages[*]}"
  exit 1
fi
echo "[run_all] All stages succeeded (or skipped)."
exit 0

