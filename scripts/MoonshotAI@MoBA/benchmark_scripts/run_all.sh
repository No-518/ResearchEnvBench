#!/usr/bin/env bash
set -u -o pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py_bin="$(command -v python 2>/dev/null || true)"
if [[ -z "$py_bin" ]]; then
  py_bin="$(command -v python3 2>/dev/null || true)"
fi
if [[ -z "$py_bin" ]]; then
  echo "ERROR: python/python3 not found in PATH." >&2
  exit 1
fi

stages=(
  "pyright:benchmark_scripts/run_pyright_missing_imports.sh"
  "prepare:benchmark_scripts/prepare_assets.sh"
  "cpu:benchmark_scripts/run_cpu_entrypoint.sh"
  "cuda:benchmark_scripts/check_cuda_available.py"
  "single_gpu:benchmark_scripts/run_single_gpu_entrypoint.sh"
  "multi_gpu:benchmark_scripts/run_multi_gpu_entrypoint.sh"
  "env_size:benchmark_scripts/measure_env_size.py"
  "hallucination:benchmark_scripts/validate_agent_report.py"
  "summary:benchmark_scripts/summarize_results.py"
)

failed=()

read_stage_outcome() {
  local stage="$1"
  local results_json="$repo_root/build_output/$stage/results.json"
  if [[ ! -f "$results_json" ]]; then
    echo "failure 1 missing_stage_results"
    return 0
  fi
  "$py_bin" - <<PY 2>/dev/null || echo "failure 1 invalid_json"
import json
from pathlib import Path
p = Path(${results_json@Q})
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print("failure 1 invalid_json"); raise SystemExit(0)
status = str(data.get("status","failure"))
exit_code_raw = data.get("exit_code", 1)
try:
  exit_code = int(exit_code_raw)
except Exception:
  exit_code = 1
failure_category = str(data.get("failure_category","") or "")
print(status, exit_code, failure_category)
PY
}

run_stage() {
  local stage="$1"
  local cmd="$2"
  echo "=== Stage: $stage ==="
  (cd "$repo_root" && bash -lc "$cmd") || true
  local outcome
  outcome="$(read_stage_outcome "$stage")"
  local status exit_code category
  status="$(awk '{print $1}' <<<"$outcome")"
  exit_code="$(awk '{print $2}' <<<"$outcome")"
  category="$(cut -d' ' -f3- <<<"$outcome" | tr -d '\n')"

  echo "[stage=$stage] status=$status exit_code=$exit_code failure_category=${category:-}"
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    failed+=("$stage")
  fi
}

for entry in "${stages[@]}"; do
  stage="${entry%%:*}"
  rel="${entry#*:}"
  if [[ "$rel" == *.py ]]; then
    run_stage "$stage" "$py_bin $rel"
  else
    run_stage "$stage" "bash $rel"
  fi
done

echo "=== Final Summary ==="
if [[ ${#failed[@]} -gt 0 ]]; then
  echo "Failed stages (in order): ${failed[*]}"
  exit 1
fi
echo "All stages succeeded (skipped stages are allowed)."
exit 0
