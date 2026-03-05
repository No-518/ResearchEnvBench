#!/usr/bin/env bash
set -u

usage() {
  cat <<'EOF'
Run the full reproducible benchmark workflow in-order (never aborts early).

Order:
  pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary

Optional:
  --report-path <path>   Overrides SCIMLOPSBENCH_REPORT for all stages

Exit code:
  0 if no stages failed
  1 if any stage failed (after running all stages)
EOF
}

report_path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py_bin="$(command -v python3 || command -v python || true)"
if [[ -z "$py_bin" ]]; then
  echo "python3/python not found in PATH (required for orchestration)" >&2
  exit 1
fi

if [[ -n "$report_path" ]]; then
  export SCIMLOPSBENCH_REPORT="$report_path"
fi

failed_stages=()

stage_outcome() {
  local stage="$1"
  local res="$repo_root/build_output/$stage/results.json"
  if [[ ! -f "$res" ]]; then
    echo "failure 1 missing_stage_results"
    return 0
  fi
  "$py_bin" - "$res" <<'PY' 2>/dev/null || { echo "failure 1 invalid_json"; return 0; }
import json, sys
path = sys.argv[1]
try:
  with open(path, "r", encoding="utf-8") as f:
    d = json.load(f)
  status = d.get("status","failure")
  exit_code = int(d.get("exit_code",1))
  failure_category = d.get("failure_category","unknown")
  print(status, exit_code, failure_category)
except Exception:
  print("failure", 1, "invalid_json")
PY
}

run_stage() {
  local stage="$1"
  shift
  echo ""
  echo "===== stage: $stage ====="
  "$@" || true
  local outcome
  outcome="$(stage_outcome "$stage")"
  local status exit_code failure_category
  status="$(awk '{print $1}' <<<"$outcome")"
  exit_code="$(awk '{print $2}' <<<"$outcome")"
  failure_category="$(awk '{print $3}' <<<"$outcome")"

  echo "stage_result: status=$status exit_code=$exit_code failure_category=$failure_category"
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    failed_stages+=("$stage")
  fi
}

run_stage pyright      bash "$repo_root/benchmark_scripts/run_pyright_missing_imports.sh"
run_stage prepare      bash "$repo_root/benchmark_scripts/prepare_assets.sh"
run_stage cpu          bash "$repo_root/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage cuda         "$py_bin" "$repo_root/benchmark_scripts/check_cuda_available.py"
run_stage single_gpu   bash "$repo_root/benchmark_scripts/run_single_gpu_entrypoint.sh"
run_stage multi_gpu    bash "$repo_root/benchmark_scripts/run_multi_gpu_entrypoint.sh"
run_stage env_size     "$py_bin" "$repo_root/benchmark_scripts/measure_env_size.py"
run_stage hallucination "$py_bin" "$repo_root/benchmark_scripts/validate_agent_report.py"
run_stage summary      "$py_bin" "$repo_root/benchmark_scripts/summarize_results.py"

echo ""
echo "===== final ====="
if [[ "${#failed_stages[@]}" -gt 0 ]]; then
  echo "failed_stages: ${failed_stages[*]}"
  exit 1
fi
echo "failed_stages: (none)"
exit 0
