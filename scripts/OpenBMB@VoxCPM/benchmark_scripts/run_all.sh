#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root" || exit 1

export PYTHONDONTWRITEBYTECODE=1

# Keep all caches inside the allowed benchmark_assets tree.
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HOME="$repo_root/benchmark_assets/cache/hf_home"
export HF_HUB_CACHE="$repo_root/benchmark_assets/cache/hf_hub"
export HUGGINGFACE_HUB_CACHE="$repo_root/benchmark_assets/cache/hf_hub"
export HF_DATASETS_CACHE="$repo_root/benchmark_assets/cache/hf_datasets"
export TRANSFORMERS_CACHE="$repo_root/benchmark_assets/cache/hf_transformers"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export MPLCONFIGDIR="$repo_root/benchmark_assets/cache/matplotlib"
export PIP_CACHE_DIR="$repo_root/benchmark_assets/cache/pip"
export PIP_DISABLE_PIP_VERSION_CHECK=1

PYBIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYBIN" ]]; then
  echo "ERROR: python not found in PATH (needed to parse stage results)." >&2
  exit 1
fi

# Normalize SCIMLOPSBENCH_REPORT: allow passing a directory (use <dir>/report.json).
normalize_report_path() {
  local p="$1"
  if [[ -n "$p" && -d "$p" ]]; then
    echo "$p/report.json"
  else
    echo "$p"
  fi
}

if [[ -n "${SCIMLOPSBENCH_REPORT:-}" ]]; then
  export SCIMLOPSBENCH_REPORT
  SCIMLOPSBENCH_REPORT="$(normalize_report_path "$SCIMLOPSBENCH_REPORT")"
fi

# Best-effort: set SCIMLOPSBENCH_PYTHON from the agent report if available.
report_path="$(normalize_report_path "${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}")"
if [[ -z "${SCIMLOPSBENCH_PYTHON:-}" && -f "$report_path" ]]; then
  set +u
  resolved="$("$PYBIN" - <<PY 2>/dev/null || true
import json, os
from pathlib import Path
raw = os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"
p = Path(raw)
if p.is_dir():
  p = p / "report.json"
try:
  d=json.loads(p.read_text(encoding="utf-8"))
  print(d.get("python_path",""))
except Exception:
  print("")
PY
)"
  set -u
  resolved="${resolved//$'\r'/}"
  if [[ -n "$resolved" ]]; then
    export SCIMLOPSBENCH_PYTHON="$resolved"
  fi
fi

if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" && -x "${SCIMLOPSBENCH_PYTHON}" ]]; then
  PYBIN="$SCIMLOPSBENCH_PYTHON"
fi

read_stage_outcome() {
  local stage="$1"
  local results="build_output/$stage/results.json"
  if [[ ! -f "$results" ]]; then
    echo "failure 1 missing_stage_results"
    return 0
  fi
  "$PYBIN" - <<PY "$results" 2>/dev/null || echo "failure 1 invalid_json"
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
d=json.loads(p.read_text(encoding="utf-8"))
status=str(d.get("status","failure"))
try:
  exit_code=int(d.get("exit_code", 1))
except Exception:
  exit_code=1
failure_category=str(d.get("failure_category","unknown"))
print(status, exit_code, failure_category)
PY
}

run_stage() {
  local stage="$1"; shift
  echo "=== Stage: $stage ==="
  "$@" || true
  local outcome
  outcome="$(read_stage_outcome "$stage")"
  local status exit_code failure_category
  status="$(printf "%s" "$outcome" | awk '{print $1}')"
  exit_code="$(printf "%s" "$outcome" | awk '{print $2}')"
  failure_category="$(printf "%s" "$outcome" | awk '{print $3}')"
  echo "[run_all] $stage status=$status exit_code=$exit_code failure_category=$failure_category"
  if [[ "$status" == "failure" || "$exit_code" -eq 1 ]]; then
    FAILED_STAGES+=("$stage")
  fi
}

FAILED_STAGES=()

# 1) pyright
if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  run_stage "pyright" bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root" --python "$SCIMLOPSBENCH_PYTHON"
else
  run_stage "pyright" bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root"
fi

# 2) prepare
run_stage "prepare" bash benchmark_scripts/prepare_assets.sh

# 3) cpu
run_stage "cpu" bash benchmark_scripts/run_cpu_entrypoint.sh

# 4) cuda
run_stage "cuda" "$PYBIN" benchmark_scripts/check_cuda_available.py

# 5) single gpu
run_stage "single_gpu" bash benchmark_scripts/run_single_gpu_entrypoint.sh

# 6) multi gpu
run_stage "multi_gpu" bash benchmark_scripts/run_multi_gpu_entrypoint.sh

# 7) env size
run_stage "env_size" "$PYBIN" benchmark_scripts/measure_env_size.py --report-path "$report_path"

# 8) hallucination validation
run_stage "hallucination" "$PYBIN" benchmark_scripts/validate_agent_report.py --report-path "$report_path"

# 9) summary
run_stage "summary" "$PYBIN" benchmark_scripts/summarize_results.py

echo "=== Final Summary ==="
if [[ ${#FAILED_STAGES[@]} -gt 0 ]]; then
  echo "Failed stages (in order): ${FAILED_STAGES[*]}"
  exit 1
fi
echo "All stages succeeded (or were skipped)."
exit 0
