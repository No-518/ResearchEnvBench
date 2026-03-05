#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 1

failed=()

resolve_scimlopsbench_python() {
  local report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
  local py=""
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    py="$SCIMLOPSBENCH_PYTHON"
  elif [[ -f "$report_path" ]]; then
    py="$(jq -r '.python_path // empty' "$report_path" 2>/dev/null || true)"
  fi
  if [[ -n "$py" ]]; then
    export SCIMLOPSBENCH_PYTHON="$py"
  fi
}

stage_failed() {
  local stage="$1"
  local results="build_output/${stage}/results.json"
  local status=""
  local exit_code=""
  if [[ -f "$results" ]]; then
    status="$(jq -r '.status // empty' "$results" 2>/dev/null)" || status=""
    exit_code="$(jq -r '.exit_code // empty' "$results" 2>/dev/null)" || exit_code=""
    if [[ -z "$status" && -z "$exit_code" ]]; then
      status="failure"
      exit_code="1"
    fi
  else
    status="failure"
    exit_code="1"
  fi
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    failed+=("$stage")
  fi
}

run_stage() {
  local stage="$1"; shift
  echo "=== Stage: $stage ==="
  "$@" || true
  stage_failed "$stage"
}

resolve_scimlopsbench_python
if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root" --python "$SCIMLOPSBENCH_PYTHON" --mode system --install-pyright
else
  run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root" --mode system --install-pyright
fi
run_stage prepare bash benchmark_scripts/prepare_assets.sh
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh

run_stage cuda "${SCIMLOPSBENCH_PYTHON:-python}" benchmark_scripts/check_cuda_available.py
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage env_size "${SCIMLOPSBENCH_PYTHON:-python}" benchmark_scripts/measure_env_size.py
run_stage hallucination "${SCIMLOPSBENCH_PYTHON:-python}" benchmark_scripts/validate_agent_report.py
run_stage summary "${SCIMLOPSBENCH_PYTHON:-python}" benchmark_scripts/summarize_results.py

if [[ ${#failed[@]} -gt 0 ]]; then
  echo "=== FAILED STAGES (in order) ==="
  printf '%s\n' "${failed[@]}"
  exit 1
fi
echo "=== ALL STAGES SUCCEEDED (or skipped) ==="
exit 0
