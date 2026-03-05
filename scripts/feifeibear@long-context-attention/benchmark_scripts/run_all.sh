#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export PYTHONDONTWRITEBYTECODE=1
export XDG_CACHE_HOME="$REPO_ROOT/benchmark_assets/cache/xdg"
export PIP_CACHE_DIR="$REPO_ROOT/benchmark_assets/cache/pip"
export TORCH_HOME="$REPO_ROOT/benchmark_assets/cache/torch"
export HF_HOME="$REPO_ROOT/benchmark_assets/cache/huggingface"
export TRANSFORMERS_CACHE="$REPO_ROOT/benchmark_assets/cache/huggingface"
export HOME="$REPO_ROOT/benchmark_assets/cache/home"
export TMPDIR="$REPO_ROOT/benchmark_assets/cache/tmp"
mkdir -p "$XDG_CACHE_HOME" "$PIP_CACHE_DIR" "$TORCH_HOME" "$HF_HOME" "$HOME" "$TMPDIR"

PYTHON_BIN="$(python3 "$REPO_ROOT/benchmark_scripts/runner.py" resolve-python --requires-python 2>/dev/null || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    PYTHON_BIN="python"
  fi
fi

FAILED_STAGES=()

read_stage_result() {
  local stage="$1"
  local results_path="$REPO_ROOT/build_output/$stage/results.json"
  if [[ ! -f "$results_path" ]]; then
    echo "failure 1 missing_stage_results"
    return 0
  fi
  "$PYTHON_BIN" - <<'PY' "$results_path"
import json, sys
p = sys.argv[1]
try:
  with open(p, "r", encoding="utf-8") as f:
    d = json.load(f)
except Exception:
  print("failure 1 invalid_json")
  raise SystemExit(0)
status = d.get("status", "failure")
raw_exit = d.get("exit_code", 1)
try:
  exit_code = int(raw_exit)
except Exception:
  exit_code = 1
failure_category = d.get("failure_category", "unknown")
print(status, exit_code, failure_category)
PY
}

record_failure_if_needed() {
  local stage="$1"
  local status exit_code failure_category
  read -r status exit_code failure_category < <(read_stage_result "$stage")
  if [[ "$status" == "skipped" ]]; then
    echo "[run_all] stage=$stage status=$status exit_code=$exit_code"
    return 0
  fi
  if [[ "$status" == "failure" || "$exit_code" -eq 1 ]]; then
    FAILED_STAGES+=("$stage")
    echo "[run_all] stage=$stage status=$status exit_code=$exit_code failure_category=$failure_category"
  else
    echo "[run_all] stage=$stage status=$status exit_code=$exit_code"
  fi
}

cd "$REPO_ROOT" || exit 1

echo "[run_all] Using python: $PYTHON_BIN"
echo "[run_all] Repo root: $REPO_ROOT"

echo "[run_all] 1/9 pyright"
bash "$REPO_ROOT/benchmark_scripts/run_pyright_missing_imports.sh" || true
record_failure_if_needed "pyright"

echo "[run_all] 2/9 prepare"
bash "$REPO_ROOT/benchmark_scripts/prepare_assets.sh" || true
record_failure_if_needed "prepare"

echo "[run_all] 3/9 cpu"
bash "$REPO_ROOT/benchmark_scripts/run_cpu_entrypoint.sh" || true
record_failure_if_needed "cpu"

echo "[run_all] 4/9 cuda"
"$PYTHON_BIN" "$REPO_ROOT/benchmark_scripts/check_cuda_available.py" || true
record_failure_if_needed "cuda"

echo "[run_all] 5/9 single_gpu"
bash "$REPO_ROOT/benchmark_scripts/run_single_gpu_entrypoint.sh" || true
record_failure_if_needed "single_gpu"

echo "[run_all] 6/9 multi_gpu"
bash "$REPO_ROOT/benchmark_scripts/run_multi_gpu_entrypoint.sh" || true
record_failure_if_needed "multi_gpu"

echo "[run_all] 7/9 env_size"
"$PYTHON_BIN" "$REPO_ROOT/benchmark_scripts/measure_env_size.py" || true
record_failure_if_needed "env_size"

echo "[run_all] 8/9 hallucination"
"$PYTHON_BIN" "$REPO_ROOT/benchmark_scripts/validate_agent_report.py" || true
record_failure_if_needed "hallucination"

echo "[run_all] 9/9 summary"
"$PYTHON_BIN" "$REPO_ROOT/benchmark_scripts/summarize_results.py" || true
record_failure_if_needed "summary"

if [[ ${#FAILED_STAGES[@]} -gt 0 ]]; then
  echo "[run_all] FAILED_STAGES (in order): ${FAILED_STAGES[*]}"
  exit 1
fi

echo "[run_all] All stages succeeded (skipped not counted as failure)."
exit 0
