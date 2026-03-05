#!/usr/bin/env bash
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

REPORT_PATH="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"

failures=()

resolve_python_from_report() {
  python - <<'PY' 2>/dev/null || true
import json, os, pathlib, sys
p = pathlib.Path(os.environ.get("SCIMLOPSBENCH_REPORT", "/opt/scimlopsbench/report.json"))
try:
  obj = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  sys.exit(0)
py = obj.get("python_path")
if isinstance(py, str) and py:
  print(py)
PY
}

RESOLVED_PY="$(resolve_python_from_report)"
PY_STAGE="${RESOLVED_PY:-python}"

stage_outcome() {
  local stage="$1"
  python - <<PY
import json, pathlib, sys
p = pathlib.Path("build_output") / ${stage@Q} / "results.json"
if not p.exists():
  print("failure 1")
  sys.exit(0)
try:
  obj = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print("failure 1")
  sys.exit(0)
status = str(obj.get("status", "failure"))
raw_exit = obj.get("exit_code", 1)
exit_code = int(raw_exit) if raw_exit is not None else 1
print(status, exit_code)
PY
}

run_stage() {
  local stage="$1"
  shift
  echo "=== [run_all] stage: $stage ==="
  echo "command: $*"
  "$@"
  local rc=$?
  local status exit_code
  read -r status exit_code < <(stage_outcome "$stage")
  echo "result: status=$status exit_code=$exit_code script_rc=$rc"
  if [[ "$status" == "failure" || "$exit_code" -eq 1 ]]; then
    failures+=("$stage")
  fi
}

run_stage "pyright" bash "$REPO_ROOT/benchmark_scripts/run_pyright_missing_imports.sh" --repo "$REPO_ROOT" --python "$PY_STAGE"
run_stage "prepare" bash "$REPO_ROOT/benchmark_scripts/prepare_assets.sh" --out-dir "$REPO_ROOT/build_output/prepare" --report-path "$REPORT_PATH"
run_stage "cpu" bash "$REPO_ROOT/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage "cuda" "$PY_STAGE" "$REPO_ROOT/benchmark_scripts/check_cuda_available.py"
run_stage "single_gpu" bash "$REPO_ROOT/benchmark_scripts/run_single_gpu_entrypoint.sh" --report-path "$REPORT_PATH"
run_stage "multi_gpu" bash "$REPO_ROOT/benchmark_scripts/run_multi_gpu_entrypoint.sh" --report-path "$REPORT_PATH"
run_stage "env_size" "$PY_STAGE" "$REPO_ROOT/benchmark_scripts/measure_env_size.py" --report-path "$REPORT_PATH"
run_stage "hallucination" "$PY_STAGE" "$REPO_ROOT/benchmark_scripts/validate_agent_report.py" --report-path "$REPORT_PATH"
run_stage "summary" "$PY_STAGE" "$REPO_ROOT/benchmark_scripts/summarize_results.py"

if [[ "${#failures[@]}" -gt 0 ]]; then
  echo "=== [run_all] FAILED STAGES (in order) ==="
  for s in "${failures[@]}"; do
    echo "- $s"
  done
  exit 1
fi

echo "=== [run_all] ALL STAGES PASSED ==="
exit 0
