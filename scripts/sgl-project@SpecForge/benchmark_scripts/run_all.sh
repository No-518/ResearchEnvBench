#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

host_python="$(command -v python3 || command -v python || true)"
if [[ -z "$host_python" ]]; then
  echo "[run_all] ERROR: python3/python not found in PATH (needed to read stage results.json)" >&2
  exit 1
fi

failed_stages=()

stage_outcome() {
  local stage="$1"
  local res_path="$repo_root/build_output/$stage/results.json"
  if [[ ! -f "$res_path" ]]; then
    echo "failure 1 missing_stage_results"
    return 0
  fi
  "$host_python" - "$res_path" <<'PY' 2>/dev/null || { echo "failure 1 invalid_json"; return 0; }
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p, "r", encoding="utf-8"))
except Exception:
    print("failure 1 invalid_json")
    raise SystemExit(0)
status = d.get("status", "failure")
exit_code = d.get("exit_code", 1)
failure_category = d.get("failure_category", "unknown")
print(f"{status} {exit_code} {failure_category}")
PY
}

run_stage() {
  local stage="$1"; shift
  echo "[run_all] ===== stage=$stage ====="
  "$@" || true
  read -r status exit_code failure_category < <(stage_outcome "$stage")
  if [[ "$status" == "skipped" ]]; then
    echo "[run_all] stage=$stage status=skipped"
    return 0
  fi
  if [[ "$status" == "failure" || "${exit_code:-1}" == "1" ]]; then
    failed_stages+=("$stage")
    echo "[run_all] stage=$stage status=failure failure_category=${failure_category:-unknown}"
  else
    echo "[run_all] stage=$stage status=success"
  fi
}

run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root"
run_stage prepare bash benchmark_scripts/prepare_assets.sh
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh
run_stage cuda "$host_python" benchmark_scripts/check_cuda_available.py
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage env_size "$host_python" benchmark_scripts/measure_env_size.py
run_stage hallucination "$host_python" benchmark_scripts/validate_agent_report.py
run_stage summary "$host_python" benchmark_scripts/summarize_results.py

if [[ "${#failed_stages[@]}" -gt 0 ]]; then
  echo "[run_all] FAILED stages (in order): ${failed_stages[*]}"
  exit 1
fi
echo "[run_all] All stages succeeded (skipped stages not counted as failure)."
exit 0
