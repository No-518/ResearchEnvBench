#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run the full reproducible benchmark workflow (does not stop on failures).

Order:
  pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary

Optional:
  --report-path <path>   Override /opt/scimlopsbench/report.json (also sets SCIMLOPSBENCH_REPORT)

Example:
  bash benchmark_scripts/run_all.sh
  bash benchmark_scripts/run_all.sh --report-path /opt/scimlopsbench/report.json
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

report_path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -n "$report_path" ]]; then
  export SCIMLOPSBENCH_REPORT="$report_path"
fi

failed_stages=()

read_stage_outcome() {
  local results_json="$1"
  python - <<PY 2>/dev/null || true
import json, pathlib, sys
p = pathlib.Path(${results_json@Q})
if not p.exists():
    print("failure 1")
    sys.exit(0)
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print("failure 1")
    sys.exit(0)
status = str(data.get("status", "failure"))
exit_code = int(data.get("exit_code", 1) or 1)
print(f"{status} {exit_code}")
PY
}

record_outcome() {
  local stage="$1"
  local results_json="$repo_root/build_output/$stage/results.json"
  local outcome
  outcome="$(read_stage_outcome "$results_json")"
  local status exit_code
  status="$(awk '{print $1}' <<<"$outcome")"
  exit_code="$(awk '{print $2}' <<<"$outcome")"
  if [[ -z "$status" || -z "$exit_code" ]]; then
    status="failure"
    exit_code="1"
  fi
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    failed_stages+=("$stage")
  fi
  echo "[run_all] stage=$stage status=$status exit_code=$exit_code results=$results_json"
}

get_report_python() {
  python - <<'PY' 2>/dev/null || true
import json, os, pathlib
report_path = os.environ.get("SCIMLOPSBENCH_REPORT", "/opt/scimlopsbench/report.json")
p = pathlib.Path(report_path)
if not p.exists():
    raise SystemExit(0)
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
v = data.get("python_path")
if isinstance(v, str) and v.strip():
    print(v.strip())
PY
}

run_stage() {
  local stage="$1"
  shift
  echo "================================================================================"
  echo "[run_all] Running stage: $stage"
  echo "--------------------------------------------------------------------------------"
  "$@" || true
  record_outcome "$stage"
}

run_stage "pyright" bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root"
run_stage "prepare" bash benchmark_scripts/prepare_assets.sh --repo-root "$repo_root"
run_stage "cpu" bash benchmark_scripts/run_cpu_entrypoint.sh

# CUDA check should run inside the agent-reported python environment when possible.
cuda_py="$(get_report_python)"
if [[ -n "$cuda_py" && -x "$cuda_py" ]]; then
  run_stage "cuda" "$cuda_py" benchmark_scripts/check_cuda_available.py
else
  run_stage "cuda" python benchmark_scripts/check_cuda_available.py
fi

run_stage "single_gpu" bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage "multi_gpu" bash benchmark_scripts/run_multi_gpu_entrypoint.sh

env_size_cmd=(python benchmark_scripts/measure_env_size.py)
hallucination_cmd=(python benchmark_scripts/validate_agent_report.py)
if [[ -n "$report_path" ]]; then
  env_size_cmd+=(--report-path "$report_path")
  hallucination_cmd+=(--report-path "$report_path")
fi

run_stage "env_size" "${env_size_cmd[@]}"
run_stage "hallucination" "${hallucination_cmd[@]}"
run_stage "summary" python benchmark_scripts/summarize_results.py

echo "================================================================================"
if [[ "${#failed_stages[@]}" -gt 0 ]]; then
  echo "[run_all] FAILED stages (execution order): ${failed_stages[*]}"
  exit 1
fi
echo "[run_all] All stages succeeded (skipped stages are not counted as failures)."
exit 0
