#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_JSON="$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)"

failed_stages=()

read_stage_field() {
  local stage="$1"
  local field="$2"
  local path="${REPO_ROOT}/build_output/${stage}/results.json"
  if [[ ! -f "${path}" ]]; then
    echo "__missing__"
    return 0
  fi
  "${PY_JSON}" - <<PY "${path}" "${field}" 2>/dev/null || echo "__invalid__"
import json, sys
p, field = sys.argv[1], sys.argv[2]
obj = json.load(open(p, "r", encoding="utf-8"))
val = obj.get(field, "__missing__")
print(val if val is not None else "__missing__")
PY
}

record_outcome() {
  local stage="$1"
  local status exit_code
  status="$(read_stage_field "${stage}" "status")"
  exit_code="$(read_stage_field "${stage}" "exit_code")"

  if [[ "${status}" == "__missing__" || "${status}" == "__invalid__" ]]; then
    failed_stages+=("${stage}")
    echo "[run_all] ${stage}: failed (missing/invalid results.json)"
    return 0
  fi

  if [[ "${status}" == "skipped" ]]; then
    echo "[run_all] ${stage}: skipped"
    return 0
  fi

  if [[ "${status}" == "failure" || "${exit_code}" == "1" ]]; then
    failed_stages+=("${stage}")
    echo "[run_all] ${stage}: failed (status=${status} exit_code=${exit_code})"
    return 0
  fi

  echo "[run_all] ${stage}: success"
}

run_stage() {
  local stage="$1"; shift
  echo "===================="
  echo "[run_all] stage=${stage}"
  echo "[run_all] cmd=$*"
  echo "--------------------"
  set +e
  "$@"
  local rc=$?
  set -e
  echo "[run_all] stage=${stage} raw_exit_code=${rc}"
  record_outcome "${stage}"
}

mkdir -p "${REPO_ROOT}/build_output"
mkdir -p "${REPO_ROOT}/benchmark_assets/cache" "${REPO_ROOT}/benchmark_assets/dataset" "${REPO_ROOT}/benchmark_assets/model"

run_stage "pyright" bash "${REPO_ROOT}/benchmark_scripts/run_pyright_missing_imports.sh" --repo "${REPO_ROOT}"
run_stage "prepare" bash "${REPO_ROOT}/benchmark_scripts/prepare_assets.sh"
run_stage "cpu" bash "${REPO_ROOT}/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage "cuda" "${PY_JSON}" "${REPO_ROOT}/benchmark_scripts/check_cuda_available.py"
run_stage "single_gpu" bash "${REPO_ROOT}/benchmark_scripts/run_single_gpu_entrypoint.sh"
run_stage "multi_gpu" bash "${REPO_ROOT}/benchmark_scripts/run_multi_gpu_entrypoint.sh"
run_stage "env_size" "${PY_JSON}" "${REPO_ROOT}/benchmark_scripts/measure_env_size.py"
run_stage "hallucination" "${PY_JSON}" "${REPO_ROOT}/benchmark_scripts/validate_agent_report.py"
run_stage "summary" "${PY_JSON}" "${REPO_ROOT}/benchmark_scripts/summarize_results.py"

echo "===================="
if [[ "${#failed_stages[@]}" -gt 0 ]]; then
  echo "[run_all] FAILED stages (execution order): ${failed_stages[*]}"
  exit 1
fi
echo "[run_all] All stages succeeded."
exit 0

