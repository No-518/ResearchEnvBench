#!/usr/bin/env bash
set -u

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

stages=(pyright prepare cpu cuda single_gpu multi_gpu env_size hallucination summary)
for s in "${stages[@]}"; do
  mkdir -p "build_output/$s"
done

failed_stages=()

read_stage_outcome() {
  local stage="$1"
  local results="build_output/${stage}/results.json"
  if [[ ! -f "$results" ]]; then
    echo "missing results.json for stage=$stage ($results)"
    return 2
  fi
  python3 - <<PY "$results"
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
try:
    d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print("failure")
    sys.exit(0)
status = str(d.get("status","failure"))
exit_code = int(d.get("exit_code", 1))
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
  echo
  echo "===================="
  echo "Stage: $stage"
  echo "Cmd: $*"
  echo "===================="
  set +e
  "$@"
  local rc=$?
  set -e
  local outcome
  outcome="$(read_stage_outcome "$stage" 2>/dev/null || echo "failure")"
  if [[ "$outcome" == "failure" ]]; then
    failed_stages+=("$stage")
  fi
  echo "[run_all] stage=$stage rc=$rc outcome=$outcome"
}

set -e

# Resolve report python for python-script stages (best-effort). If missing, fall back to python3 so scripts can
# still emit failure results.
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
py_from_report="$(python3 - <<PY 2>/dev/null || true
import json, pathlib
p = pathlib.Path(${report_path@Q})
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("python_path","") or "")
except Exception:
    print("")
PY
)"
if [[ -n "$py_from_report" && -x "$py_from_report" ]]; then
  PYTHON_EXE="$py_from_report"
else
  PYTHON_EXE="python3"
fi
if [[ -z "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  export SCIMLOPSBENCH_PYTHON="$PYTHON_EXE"
fi

run_stage "pyright" bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root"
run_stage "prepare" bash benchmark_scripts/prepare_assets.sh
run_stage "cpu" bash benchmark_scripts/run_cpu_entrypoint.sh
run_stage "cuda" "$PYTHON_EXE" benchmark_scripts/check_cuda_available.py
run_stage "single_gpu" bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage "multi_gpu" bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage "env_size" "$PYTHON_EXE" benchmark_scripts/measure_env_size.py
run_stage "hallucination" "$PYTHON_EXE" benchmark_scripts/validate_agent_report.py
run_stage "summary" "$PYTHON_EXE" benchmark_scripts/summarize_results.py

echo
echo "===================="
if [[ "${#failed_stages[@]}" -gt 0 ]]; then
  echo "FAILED STAGES (in order): ${failed_stages[*]}"
  exit 1
else
  echo "All stages succeeded (skipped stages not counted as failures)."
  exit 0
fi
