#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sys_python="$(command -v python3 || command -v python || true)"

if [[ -z "${sys_python}" ]]; then
  echo "ERROR: python3/python not found in PATH" >&2
  exit 1
fi

failed_stages=()

stage_outcome() {
  local stage="$1"
  local results_path="${repo_root}/build_output/${stage}/results.json"
  if [[ ! -f "${results_path}" ]]; then
    echo "failure 1 missing_stage_results"
    return 0
  fi
  "${sys_python}" - <<'PY' "${results_path}" || true
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not_object")
    status = data.get("status", "failure")
    exit_code = int(data.get("exit_code", 1) or 0)
    failure_category = data.get("failure_category", "")
    print(status, exit_code, failure_category)
except Exception:
    print("failure", 1, "invalid_json")
PY
}

run_stage() {
  local stage="$1"
  shift
  echo ""
  echo "=== Stage: ${stage} ==="
  set +e
  "$@"
  local rc=$?
  set -e

  read -r status exit_code failure_category < <(stage_outcome "${stage}")

  echo "[run_all] ${stage}: script_rc=${rc} status=${status} exit_code=${exit_code} failure_category=${failure_category}"

  if [[ "${status}" == "failure" || "${exit_code}" == "1" ]]; then
    failed_stages+=("${stage}")
  fi
}

set -e

run_stage "pyright" bash "${repo_root}/benchmark_scripts/run_pyright_missing_imports.sh" --repo "${repo_root}"
run_stage "prepare" bash "${repo_root}/benchmark_scripts/prepare_assets.sh"
run_stage "cpu" bash "${repo_root}/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage "cuda" "${sys_python}" "${repo_root}/benchmark_scripts/check_cuda_available.py"
run_stage "single_gpu" bash "${repo_root}/benchmark_scripts/run_single_gpu_entrypoint.sh"
run_stage "multi_gpu" bash "${repo_root}/benchmark_scripts/run_multi_gpu_entrypoint.sh"
run_stage "env_size" "${sys_python}" "${repo_root}/benchmark_scripts/measure_env_size.py"
run_stage "hallucination" "${sys_python}" "${repo_root}/benchmark_scripts/validate_agent_report.py"
run_stage "summary" "${sys_python}" "${repo_root}/benchmark_scripts/summarize_results.py"

echo ""
if [[ "${#failed_stages[@]}" -gt 0 ]]; then
  echo "FAILED STAGES (in order): ${failed_stages[*]}"
  exit 1
fi
echo "ALL STAGES PASSED"
exit 0

