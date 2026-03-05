#!/usr/bin/env bash
set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_SYS="$(command -v python3 || command -v python || true)"

if [[ -z "${PY_SYS}" ]]; then
  echo "[run_all] No python found in PATH." >&2
  exit 1
fi

stages=(
  "pyright"
  "prepare"
  "cpu"
  "cuda"
  "single_gpu"
  "multi_gpu"
  "env_size"
  "hallucination"
  "summary"
)

failures=()

read_stage_outcome() {
  local stage="$1"
  local results_path="${REPO_ROOT}/build_output/${stage}/results.json"
  if [[ ! -f "${results_path}" ]]; then
    echo "failure missing_stage_results"
    return 0
  fi
  "${PY_SYS}" - <<'PY' "${results_path}"
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p, "r", encoding="utf-8"))
except Exception:
    print("failure invalid_json"); sys.exit(0)
status = str(d.get("status", "failure"))
try:
    exit_code_val = d.get("exit_code", 1)
    exit_code = int(exit_code_val) if exit_code_val is not None else 1
except Exception:
    exit_code = 1
if status == "skipped":
    print("skipped")
elif status == "failure" or exit_code == 1:
    print("failure")
else:
    print("success")
PY
}

run_stage() {
  local stage="$1"
  shift
  echo "========== [run_all] stage=${stage} =========="
  "$@" || true
  local outcome
  outcome="$(read_stage_outcome "${stage}")"
  case "${outcome}" in
    skipped*)
      echo "[run_all] ${stage}: skipped"
      ;;
    success*)
      echo "[run_all] ${stage}: success"
      ;;
    *)
      echo "[run_all] ${stage}: failure"
      failures+=("${stage}")
      ;;
  esac
}

mkdir -p "${REPO_ROOT}/build_output"
mkdir -p "${REPO_ROOT}/benchmark_assets/cache" "${REPO_ROOT}/benchmark_assets/dataset" "${REPO_ROOT}/benchmark_assets/model"

run_stage "pyright" bash "${REPO_ROOT}/benchmark_scripts/run_pyright_missing_imports.sh"
run_stage "prepare" bash "${REPO_ROOT}/benchmark_scripts/prepare_assets.sh"
run_stage "cpu" bash "${REPO_ROOT}/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage "cuda" "${PY_SYS}" "${REPO_ROOT}/benchmark_scripts/check_cuda_available.py"
run_stage "single_gpu" bash "${REPO_ROOT}/benchmark_scripts/run_single_gpu_entrypoint.sh"
run_stage "multi_gpu" bash "${REPO_ROOT}/benchmark_scripts/run_multi_gpu_entrypoint.sh"
run_stage "env_size" "${PY_SYS}" "${REPO_ROOT}/benchmark_scripts/measure_env_size.py"
run_stage "hallucination" "${PY_SYS}" "${REPO_ROOT}/benchmark_scripts/validate_agent_report.py"
run_stage "summary" "${PY_SYS}" "${REPO_ROOT}/benchmark_scripts/summarize_results.py"

echo "========== [run_all] final =========="
if [[ ${#failures[@]} -gt 0 ]]; then
  echo "[run_all] Failed stages (in order): ${failures[*]}"
  exit 1
fi
echo "[run_all] All stages succeeded (skipped stages ignored)."
exit 0
