#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_PYTHON="$(command -v python3 || command -v python || true)"
if [[ -z "$HOST_PYTHON" ]]; then
  echo "python3/python missing in PATH; cannot orchestrate run_all" >&2
  exit 1
fi

export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

FAILED_STAGES=()

read_stage_outcome() {
  local stage="$1"
  local results_path="$ROOT_DIR/build_output/${stage}/results.json"
  "$HOST_PYTHON" - "$results_path" <<'PY'
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
if not p.exists():
    print("failure\t1\tmissing_stage_results")
    raise SystemExit(0)

try:
    d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print("failure\t1\tinvalid_json")
    raise SystemExit(0)

status = str(d.get("status", "failure"))
exit_code = d.get("exit_code", 1)
try:
    exit_code = int(exit_code)
except Exception:
    exit_code = 1
failure_category = str(d.get("failure_category", "unknown"))
print(f"{status}\t{exit_code}\t{failure_category}")
PY
}

run_stage() {
  local stage="$1"; shift
  echo "===== STAGE: ${stage} ====="
  "$@" || true

  local outcome
  outcome="$(read_stage_outcome "$stage")"
  local status exit_code failure_category
  IFS=$'\t' read -r status exit_code failure_category <<<"$outcome"

  echo "[run_all] ${stage}: status=${status} exit_code=${exit_code} failure_category=${failure_category}"

  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    FAILED_STAGES+=("$stage")
  fi
}

run_stage pyright bash "$ROOT_DIR/benchmark_scripts/run_pyright_missing_imports.sh" --repo "$ROOT_DIR" --install-pyright
run_stage prepare bash "$ROOT_DIR/benchmark_scripts/prepare_assets.sh" --offline-ok
run_stage cpu bash "$ROOT_DIR/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage cuda "$HOST_PYTHON" "$ROOT_DIR/benchmark_scripts/check_cuda_available.py"
run_stage single_gpu bash "$ROOT_DIR/benchmark_scripts/run_single_gpu_entrypoint.sh"
run_stage multi_gpu bash "$ROOT_DIR/benchmark_scripts/run_multi_gpu_entrypoint.sh"
run_stage env_size "$HOST_PYTHON" "$ROOT_DIR/benchmark_scripts/measure_env_size.py"
run_stage hallucination "$HOST_PYTHON" "$ROOT_DIR/benchmark_scripts/validate_agent_report.py"
run_stage summary "$HOST_PYTHON" "$ROOT_DIR/benchmark_scripts/summarize_results.py"

echo "===== FINAL (run_all) ====="
if [[ "${#FAILED_STAGES[@]}" -gt 0 ]]; then
  echo "Failed stages (in order): ${FAILED_STAGES[*]}"
  exit 1
fi
echo "All stages succeeded or were skipped."
exit 0
