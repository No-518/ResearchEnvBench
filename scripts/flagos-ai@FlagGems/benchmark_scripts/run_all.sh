#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python_bin="python3"
command -v python3 >/dev/null 2>&1 || python_bin="python"

failed_stages=()

read_stage_status() {
  local stage="$1"
  local results_json="$repo_root/build_output/$stage/results.json"
  if [[ ! -f "$results_json" ]]; then
    echo "failure 1 missing_stage_results"
    return 0
  fi
  "$python_bin" - <<PY 2>/dev/null || true
import json, sys
p=${results_json@Q}
try:
  d=json.load(open(p,'r',encoding='utf-8'))
except Exception:
  print("failure 1 invalid_json")
  raise SystemExit(0)
status=str(d.get("status","failure"))
raw_exit_code=d.get("exit_code", 1)
try:
  exit_code=int(raw_exit_code) if raw_exit_code is not None else 1
except Exception:
  exit_code=1
failure_category=str(d.get("failure_category","unknown"))
print(f"{status} {exit_code} {failure_category}")
PY
}

run_stage() {
  local stage="$1"; shift
  echo "=============================="
  echo "Stage: $stage"
  echo "Command: $*"
  echo "=============================="
  "$@"
  local cmd_rc=$?

  local parsed
  parsed="$(read_stage_status "$stage" || true)"
  if [[ -z "$parsed" ]]; then
    status="failure"
    exit_code="1"
    failure_category="unknown"
  else
    read -r status exit_code failure_category <<<"$parsed"
  fi
  echo "[run_all] stage=$stage cmd_rc=$cmd_rc status=$status exit_code=$exit_code failure_category=$failure_category"

  if [[ "$status" == "failure" || "$exit_code" -eq 1 ]]; then
    failed_stages+=("$stage")
  fi
}

run_stage pyright bash "$repo_root/benchmark_scripts/run_pyright_missing_imports.sh" --repo "$repo_root" --out-dir "$repo_root/build_output/pyright"
run_stage prepare bash "$repo_root/benchmark_scripts/prepare_assets.sh"
run_stage cpu bash "$repo_root/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage cuda "$python_bin" "$repo_root/benchmark_scripts/check_cuda_available.py"
run_stage single_gpu bash "$repo_root/benchmark_scripts/run_single_gpu_entrypoint.sh"
run_stage multi_gpu bash "$repo_root/benchmark_scripts/run_multi_gpu_entrypoint.sh"
run_stage env_size "$python_bin" "$repo_root/benchmark_scripts/measure_env_size.py"
run_stage hallucination "$python_bin" "$repo_root/benchmark_scripts/validate_agent_report.py"
run_stage summary "$python_bin" "$repo_root/benchmark_scripts/summarize_results.py"

echo "=============================="
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "FAILED STAGES (in order): ${failed_stages[*]}"
  exit 1
fi
echo "ALL STAGES PASSED"
exit 0
