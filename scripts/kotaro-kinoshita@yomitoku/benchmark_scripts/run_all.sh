#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run the full benchmark workflow (does not abort on intermediate failures).

Order:
  pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary

Options:
  --report-path <path>    Default: /opt/scimlopsbench/report.json (or $SCIMLOPSBENCH_REPORT)
  --python <path>         Explicit python for stages that accept it (overrides report.json)
EOF
}

report_path=""
python_bin=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "$report_path" ]]; then
  report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
fi

resolve_python_from_report() {
  local rp="$1"
  python "$REPO_ROOT/benchmark_scripts/runner.py" resolve-python --report-path "$rp" 2>/dev/null || true
}

if [[ -z "$python_bin" ]]; then
  python_bin="$(resolve_python_from_report "$report_path")"
fi

failures=()

stage_results_path() {
  local stage="$1"
  echo "$REPO_ROOT/build_output/$stage/results.json"
}

read_stage_outcome() {
  local stage="$1"
  local path
  path="$(stage_results_path "$stage")"
  if [[ ! -f "$path" ]]; then
    echo "failure 1 missing_stage_results"
    return 0
  fi
  python - <<PY 2>/dev/null || echo "failure 1 invalid_json"
import json
from pathlib import Path
p=Path(r"""$path""")
try:
    obj=json.loads(p.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
status=obj.get("status","failure")
try:
    exit_code=int(obj.get("exit_code",1))
except Exception:
    exit_code=1
failure_category=obj.get("failure_category","unknown")
print(status, exit_code, failure_category)
PY
}

run_stage() {
  local stage="$1"
  shift
  echo ""
  echo "===== stage: $stage ====="
  "$@" || true
  outcome="$(read_stage_outcome "$stage")"
  stage_status="$(echo "$outcome" | awk '{print $1}')"
  stage_exit_code="$(echo "$outcome" | awk '{print $2}')"
  stage_failure_category="$(echo "$outcome" | awk '{print $3}')"
  echo "[run_all] $stage: status=$stage_status exit_code=$stage_exit_code failure_category=$stage_failure_category"
  if [[ "$stage_status" == "failure" || "$stage_exit_code" == "1" ]]; then
    failures+=("$stage")
  fi
}

if [[ -n "$python_bin" ]]; then
  run_stage "pyright" \
    bash "$REPO_ROOT/benchmark_scripts/run_pyright_missing_imports.sh" \
      --repo "$REPO_ROOT" \
      --out-dir "build_output/pyright" \
      --python "$python_bin"
else
  run_stage "pyright" \
    bash "$REPO_ROOT/benchmark_scripts/run_pyright_missing_imports.sh" \
      --repo "$REPO_ROOT" \
      --out-dir "build_output/pyright"
fi

if [[ -n "$python_bin" ]]; then
  run_stage "prepare" \
    bash "$REPO_ROOT/benchmark_scripts/prepare_assets.sh" \
      --report-path "$report_path" \
      --python "$python_bin"
else
  run_stage "prepare" \
    bash "$REPO_ROOT/benchmark_scripts/prepare_assets.sh" \
      --report-path "$report_path"
fi

if [[ -n "$python_bin" ]]; then
  run_stage "cpu" \
    bash "$REPO_ROOT/benchmark_scripts/run_cpu_entrypoint.sh" \
      --report-path "$report_path" \
      --python "$python_bin"
else
  run_stage "cpu" \
    bash "$REPO_ROOT/benchmark_scripts/run_cpu_entrypoint.sh" \
      --report-path "$report_path"
fi

if [[ -n "$python_bin" ]]; then
  run_stage "cuda" \
    python "$REPO_ROOT/benchmark_scripts/check_cuda_available.py" \
      --report-path "$report_path" \
      --python "$python_bin"
else
  run_stage "cuda" \
    python "$REPO_ROOT/benchmark_scripts/check_cuda_available.py" \
      --report-path "$report_path"
fi

if [[ -n "$python_bin" ]]; then
  run_stage "single_gpu" \
    bash "$REPO_ROOT/benchmark_scripts/run_single_gpu_entrypoint.sh" \
      --report-path "$report_path" \
      --python "$python_bin"
else
  run_stage "single_gpu" \
    bash "$REPO_ROOT/benchmark_scripts/run_single_gpu_entrypoint.sh" \
      --report-path "$report_path"
fi

if [[ -n "$python_bin" ]]; then
  run_stage "multi_gpu" \
    bash "$REPO_ROOT/benchmark_scripts/run_multi_gpu_entrypoint.sh" \
      --report-path "$report_path" \
      --python "$python_bin"
else
  run_stage "multi_gpu" \
    bash "$REPO_ROOT/benchmark_scripts/run_multi_gpu_entrypoint.sh" \
      --report-path "$report_path"
fi

run_stage "env_size" \
  python "$REPO_ROOT/benchmark_scripts/measure_env_size.py" \
    --report-path "$report_path"

run_stage "hallucination" \
  python "$REPO_ROOT/benchmark_scripts/validate_agent_report.py" \
    --report-path "$report_path"

run_stage "summary" \
  python "$REPO_ROOT/benchmark_scripts/summarize_results.py"

echo ""
if [[ "${#failures[@]}" -gt 0 ]]; then
  echo "[run_all] FAILED STAGES (in order): ${failures[*]}"
  exit 1
fi
echo "[run_all] All stages succeeded (skipped stages are allowed)."
exit 0
