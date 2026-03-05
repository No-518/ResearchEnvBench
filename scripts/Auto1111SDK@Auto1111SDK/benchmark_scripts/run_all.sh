#!/usr/bin/env bash
set -u

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

export PYTHONDONTWRITEBYTECODE=1

PY_BIN="python"
if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  PY_BIN="python3"
fi

stages_failed=()

run_stage() {
  local stage="$1"
  shift
  echo
  echo "========== STAGE: ${stage} =========="
  # Do not abort on failures.
  set +e
  "$@"
  local rc=$?
  set -e

  local results_path="build_output/${stage}/results.json"
  if [[ ! -f "$results_path" ]]; then
    echo "[run_all] ${stage}: results.json missing: ${results_path}"
    stages_failed+=("${stage}")
    return 0
  fi

  local outcome
  outcome="$("$PY_BIN" - <<PY
import json, pathlib, sys
p = pathlib.Path("${results_path}")
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print("failure 1")
  raise SystemExit(0)
status = data.get("status", "failure")
exit_code = data.get("exit_code", 1)
print(f"{status} {exit_code}")
PY
)"

  local status="${outcome%% *}"
  local exit_code="${outcome##* }"

  echo "[run_all] ${stage}: status=${status} exit_code=${exit_code} (cmd_rc=${rc})"

  if [[ "$status" == "skipped" ]]; then
    return 0
  fi
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    stages_failed+=("${stage}")
  fi
  return 0
}

set -e

run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root"
run_stage prepare bash benchmark_scripts/prepare_assets.sh
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh
run_stage cuda "$PY_BIN" benchmark_scripts/check_cuda_available.py
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh
run_stage env_size "$PY_BIN" benchmark_scripts/measure_env_size.py
run_stage hallucination "$PY_BIN" benchmark_scripts/validate_agent_report.py
run_stage summary "$PY_BIN" benchmark_scripts/summarize_results.py

echo
if [[ ${#stages_failed[@]} -gt 0 ]]; then
  echo "FAILED STAGES (in order): ${stages_failed[*]}"
  exit 1
fi
echo "ALL STAGES SUCCEEDED"
exit 0
