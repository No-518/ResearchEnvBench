#!/usr/bin/env bash
set -u

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Keep any Python bytecode/caches inside repo-owned benchmark_assets/cache (avoid writing __pycache__ into repo code).
mkdir -p "$repo_root/benchmark_assets/cache/pycache" "$repo_root/benchmark_assets/cache/xdg" "$repo_root/benchmark_assets/cache/torch" "$repo_root/benchmark_assets/cache/hf"
export PYTHONPYCACHEPREFIX="$repo_root/benchmark_assets/cache/pycache"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export HF_HOME="$repo_root/benchmark_assets/cache/hf"
export HUGGINGFACE_HUB_CACHE="$repo_root/benchmark_assets/cache/hf"

read_stage_outcome() {
  local stage="$1"
  local results_path="$repo_root/build_output/${stage}/results.json"
  if [[ ! -f "$results_path" ]]; then
    echo "failure 1"
    return 0
  fi
  python - <<'PY' "$results_path" 2>/dev/null || echo "failure 1"
import json
import pathlib
import sys

p = pathlib.Path(sys.argv[1])
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    status = d.get("status", "failure")
    exit_code = int(d.get("exit_code", 1))
    print(status, exit_code)
except Exception:
    print("failure", 1)
PY
}

run_stage() {
  local stage="$1"; shift
  echo "=== stage: ${stage} ==="
  # Never abort early on failures.
  "$@" || true
  local outcome
  outcome="$(read_stage_outcome "$stage")"
  local status exit_code
  status="$(echo "$outcome" | awk '{print $1}')"
  exit_code="$(echo "$outcome" | awk '{print $2}')"
  echo "[run_all] ${stage}: status=${status} exit_code=${exit_code}"

  if [[ "$status" == "skipped" ]]; then
    return 0
  fi
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    FAILED_STAGES+=("$stage")
  fi
  return 0
}

FAILED_STAGES=()

run_stage pyright bash "$repo_root/benchmark_scripts/run_pyright_missing_imports.sh" --repo "$repo_root"
run_stage prepare bash "$repo_root/benchmark_scripts/prepare_assets.sh"
run_stage cpu bash "$repo_root/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage cuda python "$repo_root/benchmark_scripts/check_cuda_available.py"
run_stage single_gpu bash "$repo_root/benchmark_scripts/run_single_gpu_entrypoint.sh"
multi_gpu_args=()
if [[ -n "${SCIMLOPSBENCH_MULTI_GPU_TIMEOUT_SEC:-}" ]]; then
  multi_gpu_args=(--timeout-sec "${SCIMLOPSBENCH_MULTI_GPU_TIMEOUT_SEC}")
fi
run_stage multi_gpu bash "$repo_root/benchmark_scripts/run_multi_gpu_entrypoint.sh" "${multi_gpu_args[@]}"
run_stage env_size python "$repo_root/benchmark_scripts/measure_env_size.py"
run_stage hallucination python "$repo_root/benchmark_scripts/validate_agent_report.py"
run_stage summary python "$repo_root/benchmark_scripts/summarize_results.py"

if [[ ${#FAILED_STAGES[@]} -gt 0 ]]; then
  echo "=== failed stages (in order) ==="
  printf '%s\n' "${FAILED_STAGES[@]}"
  exit 1
fi

echo "=== all stages succeeded (or skipped) ==="
exit 0
