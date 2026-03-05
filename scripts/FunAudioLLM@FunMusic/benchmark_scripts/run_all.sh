#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

failed_stages=()

read_stage_outcome() {
  local stage="$1"
  local results_json="$repo_root/build_output/$stage/results.json"

  if [[ ! -f "$results_json" ]]; then
    echo "failure"
    return 0
  fi

  python - <<'PY' "$results_json" 2>/dev/null || echo "failure"
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
try:
  d=json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print("failure"); raise SystemExit(0)
status=str(d.get("status","failure"))
try:
  exit_code=int(d.get("exit_code", 1))
except Exception:
  exit_code=1
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
  echo "=== [run_all] stage=$stage cmd=$* ==="
  set +e
  "$@"
  local rc=$?
  set -e

  local outcome
  outcome="$(read_stage_outcome "$stage")"
  echo "=== [run_all] stage=$stage rc=$rc outcome=$outcome ==="

  if [[ "$outcome" == "failure" ]]; then
    failed_stages+=("$stage")
  fi
}

mkdir -p "$repo_root/build_output"

run_stage pyright bash "$repo_root/benchmark_scripts/run_pyright_missing_imports.sh" --repo "$repo_root"
run_stage prepare bash "$repo_root/benchmark_scripts/prepare_assets.sh"
run_stage cpu bash "$repo_root/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage cuda python "$repo_root/benchmark_scripts/check_cuda_available.py"
run_stage single_gpu bash "$repo_root/benchmark_scripts/run_single_gpu_entrypoint.sh"
run_stage multi_gpu bash "$repo_root/benchmark_scripts/run_multi_gpu_entrypoint.sh"
run_stage env_size python "$repo_root/benchmark_scripts/measure_env_size.py"
run_stage hallucination python "$repo_root/benchmark_scripts/validate_agent_report.py"
run_stage summary python "$repo_root/benchmark_scripts/summarize_results.py"

echo "=== [run_all] done ==="
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "Failed stages (execution order): ${failed_stages[*]}"
  exit 1
fi
echo "All stages succeeded or skipped."
exit 0
