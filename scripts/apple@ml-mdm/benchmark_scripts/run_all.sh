#!/usr/bin/env bash
set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

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

failed_stages=()

stage_result_status() {
  local stage="$1"
  local results_path="${repo_root}/build_output/${stage}/results.json"
  RESULTS_PATH="$results_path" python3 - <<'PY'
import json
import os
import pathlib
p = pathlib.Path(os.environ["RESULTS_PATH"])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    status = str(data.get("status") or "")
    exit_code = int(data.get("exit_code") or 0)
    print(status)
    print(exit_code)
except FileNotFoundError:
    print("missing")
    print(1)
except Exception:
    print("invalid")
    print(1)
PY
}

run_stage() {
  local stage="$1"
  shift
  echo "=============================="
  echo "STAGE: ${stage}"
  echo "CMD: $*"
  echo "=============================="

  set +e
  "$@"
  local rc=$?
  set -e

  # Determine outcome from results.json when available (as required).
  readarray -t se < <(stage_result_status "$stage")
  local status="${se[0]:-missing}"
  local exit_code="${se[1]:-1}"

  echo "Stage ${stage} finished: script_rc=${rc} results.status=${status} results.exit_code=${exit_code}"

  if [[ "$status" == "failure" || "$exit_code" == "1" || "$status" == "missing" || "$status" == "invalid" ]]; then
    failed_stages+=("$stage")
  fi
}

set -e

run_stage "pyright" bash "${repo_root}/benchmark_scripts/run_pyright_missing_imports.sh"
run_stage "prepare" bash "${repo_root}/benchmark_scripts/prepare_assets.sh"
run_stage "cpu" bash "${repo_root}/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage "cuda" python3 "${repo_root}/benchmark_scripts/check_cuda_available.py"
run_stage "single_gpu" bash "${repo_root}/benchmark_scripts/run_single_gpu_entrypoint.sh"
run_stage "multi_gpu" bash "${repo_root}/benchmark_scripts/run_multi_gpu_entrypoint.sh"
run_stage "env_size" python3 "${repo_root}/benchmark_scripts/measure_env_size.py"
run_stage "hallucination" python3 "${repo_root}/benchmark_scripts/validate_agent_report.py"
run_stage "summary" python3 "${repo_root}/benchmark_scripts/summarize_results.py"

echo "=============================="
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "FAILED STAGES (in order): ${failed_stages[*]}"
  exit 1
fi
echo "ALL STAGES SUCCEEDED"
exit 0
