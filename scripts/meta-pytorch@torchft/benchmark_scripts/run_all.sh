#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

failed=()

REPORT_PATH="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
# Trim leading/trailing whitespace to avoid common env-var injection issues.
REPORT_PATH="$(printf '%s' "$REPORT_PATH" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
export SCIMLOPSBENCH_REPORT="$REPORT_PATH"
PYRIGHT_PYTHON=""
if [[ -f "$REPORT_PATH" ]]; then
  PYRIGHT_PYTHON="$(python3 - <<'PY' "$REPORT_PATH" 2>/dev/null || true
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p, "r", encoding="utf-8"))
except Exception:
    raise SystemExit(0)
v = d.get("python_path", "")
if isinstance(v, str) and v.strip():
    print(v.strip())
PY
)"
fi

stage_outcome() {
  local stage="$1"
  local res="build_output/$stage/results.json"
  if [[ ! -f "$res" ]]; then
    echo "failure 1 missing_stage_results"
    return 0
  fi
  python3 - <<'PY' "$res"
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p, "r", encoding="utf-8"))
except Exception:
    print("failure 1 invalid_json")
    raise SystemExit(0)
status = d.get("status", "failure")
exit_code_val = d.get("exit_code", 1)
if exit_code_val is None:
    exit_code_val = 1
try:
    exit_code = int(exit_code_val)
except Exception:
    exit_code = 1
failure_category = d.get("failure_category", "unknown")
print(status, exit_code, failure_category)
PY
}

run_stage() {
  local stage="$1"; shift
  echo "=== stage: $stage ==="
  "$@" || true
  read -r status exit_code failure_category < <(stage_outcome "$stage")
  echo "[run_all] $stage status=$status exit_code=$exit_code failure_category=$failure_category"
  if [[ "$status" == "failure" || "$exit_code" -eq 1 ]]; then
    failed+=("$stage")
  fi
}

if [[ -n "$PYRIGHT_PYTHON" ]]; then
  run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$REPO_ROOT" --out-dir build_output/pyright --python "$PYRIGHT_PYTHON"
else
  run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$REPO_ROOT" --out-dir build_output/pyright --mode system
fi
run_stage prepare bash benchmark_scripts/prepare_assets.sh
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh
run_stage cuda python3 benchmark_scripts/check_cuda_available.py
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage env_size python3 benchmark_scripts/measure_env_size.py
run_stage hallucination python3 benchmark_scripts/validate_agent_report.py
run_stage summary python3 benchmark_scripts/summarize_results.py

if [[ ${#failed[@]} -gt 0 ]]; then
  echo "=== FAILED STAGES ==="
  printf '%s\n' "${failed[@]}"
  exit 1
fi

echo "=== ALL STAGES PASSED (skips allowed) ==="
exit 0
