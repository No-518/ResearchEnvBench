#!/usr/bin/env bash
set -u
set -o pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Run the full benchmark chain (never aborts early).

Optional:
  --python <path>        Override python for python-dependent stages (highest priority).
  --report-path <path>   Override agent report path (default: /opt/scimlopsbench/report.json).
  --devices <list>       Multi-GPU devices (default: 0,1). Also accepts SCIMLOPSBENCH_MULTI_GPU_DEVICES.
EOF
}

python_override=""
report_path=""
devices=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_override="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --devices) devices="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

py_parse=""
if command -v python3 >/dev/null 2>&1; then py_parse="python3"; fi
if [[ -z "$py_parse" ]] && command -v python >/dev/null 2>&1; then py_parse="python"; fi

failed_stages=()

stage_failed() {
  local stage="$1"
  local results="build_output/$stage/results.json"
  if [[ ! -f "$results" ]]; then
    failed_stages+=("$stage")
    echo "[run_all] $stage: results.json missing -> failure"
    return 0
  fi
  if [[ -z "$py_parse" ]]; then
    failed_stages+=("$stage")
    echo "[run_all] $stage: no python to parse results.json -> failure"
    return 0
  fi
  local status=""
  local exit_code=""
  read -r status exit_code < <("$py_parse" - <<'PY' "$results" || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print("failure 1")
    raise SystemExit(0)
status = str(data.get("status", "failure"))
try:
    code = int(data.get("exit_code", 1))
except Exception:
    code = 1
print(status, code)
PY
  )

  if [[ "$status" == "skipped" ]]; then
    echo "[run_all] $stage: skipped"
    return 1
  fi
  if [[ "$status" == "failure" || "${exit_code:-1}" -eq 1 ]]; then
    failed_stages+=("$stage")
    echo "[run_all] $stage: failure"
    return 0
  fi
  echo "[run_all] $stage: success"
  return 1
}

run_stage() {
  local stage="$1"
  shift
  echo ""
  echo "===================="
  echo "[run_all] Stage: $stage"
  echo "===================="
  "$@" || true
  stage_failed "$stage" || true
}

py_args=()
[[ -n "$python_override" ]] && py_args+=(--python "$python_override")
[[ -n "$report_path" ]] && py_args+=(--report-path "$report_path")

run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root" "${py_args[@]}"
run_stage prepare bash benchmark_scripts/prepare_assets.sh "${py_args[@]}"
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh "${py_args[@]}"

# CUDA check should run in the agent-reported python environment if available.
resolved_python=""
if [[ -n "$py_parse" ]]; then
  resolved_python="$("$py_parse" benchmark_scripts/runner.py --stage cuda --task check --requires-python --print-resolved-python "${py_args[@]}" 2>/dev/null || true)"
fi
if [[ -n "$resolved_python" ]]; then
  run_stage cuda "$resolved_python" benchmark_scripts/check_cuda_available.py
else
  run_stage cuda bash -lc 'echo "Could not resolve python from report for CUDA check." >&2; mkdir -p build_output/cuda; echo "missing report python" > build_output/cuda/log.txt; echo "{\"status\":\"failure\",\"skip_reason\":\"unknown\",\"exit_code\":1,\"stage\":\"cuda\",\"task\":\"check\",\"command\":\"\",\"timeout_sec\":120,\"framework\":\"unknown\",\"assets\":{\"dataset\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"},\"model\":{\"path\":\"\",\"source\":\"\",\"version\":\"\",\"sha256\":\"\"}},\"meta\":{\"python\":\"\",\"git_commit\":\"\",\"env_vars\":{},\"decision_reason\":\"missing report python\"},\"failure_category\":\"missing_report\",\"error_excerpt\":\"missing report python\"}" > build_output/cuda/results.json; exit 1'
fi

run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh "${py_args[@]}"
multi_args=("${py_args[@]}")
if [[ -n "$devices" ]]; then
  multi_args+=(--devices "$devices")
fi
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh "${multi_args[@]}"

env_py="$py_parse"
[[ -n "$python_override" ]] && env_py="$python_override"

env_args=()
[[ -n "$report_path" ]] && env_args+=(--report-path "$report_path")

run_stage env_size "$env_py" benchmark_scripts/measure_env_size.py "${env_args[@]}"
run_stage hallucination "$env_py" benchmark_scripts/validate_agent_report.py "${env_args[@]}"
run_stage summary "$env_py" benchmark_scripts/summarize_results.py

echo ""
echo "===================="
echo "[run_all] Final"
echo "===================="
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "[run_all] Failed stages (in order): ${failed_stages[*]}"
  exit 1
fi
echo "[run_all] All stages succeeded (or were skipped)."
exit 0
