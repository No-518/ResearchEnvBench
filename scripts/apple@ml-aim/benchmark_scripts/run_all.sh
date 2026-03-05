#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run the full benchmark workflow end-to-end (no early abort on failures).

Stages (in order):
  1) pyright
  2) prepare
  3) cpu
  4) cuda
  5) single_gpu
  6) multi_gpu
  7) env_size
  8) hallucination
  9) summary

Options:
  --report-path <path>     Override report.json path (also exported as SCIMLOPSBENCH_REPORT)
  --python <path>          Override python executable (also exported as SCIMLOPSBENCH_PYTHON)
  --offline                Set SCIMLOPSBENCH_OFFLINE=1 (skip downloads; require cache)
  --model <aim-600M|aim-1B|aim-3B|aim-7B>        Forwarded to prepare_assets.sh
  --probe-layers <last|best>                      Forwarded to prepare_assets.sh

Examples:
  bash benchmark_scripts/run_all.sh
  bash benchmark_scripts/run_all.sh --report-path /opt/scimlopsbench/report.json
  bash benchmark_scripts/run_all.sh --offline
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

report_path=""
python_override=""
offline="0"
model_name=""
probe_layers=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --python)
      python_override="${2:-}"; shift 2 ;;
    --offline)
      offline="1"; shift ;;
    --model)
      model_name="${2:-}"; shift 2 ;;
    --probe-layers)
      probe_layers="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -n "$report_path" ]]; then
  export SCIMLOPSBENCH_REPORT="$report_path"
fi
if [[ -n "$python_override" ]]; then
  export SCIMLOPSBENCH_PYTHON="$python_override"
fi
if [[ "$offline" == "1" ]]; then
  export SCIMLOPSBENCH_OFFLINE=1
fi

mkdir -p "$ROOT/build_output"

resolve_python_from_report() {
  local rp="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
  python3 - "$rp" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

rp = Path(sys.argv[1])
if not rp.exists():
    sys.exit(0)
try:
    data = json.loads(rp.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)
py = data.get("python_path")
if isinstance(py, str) and py.strip():
    print(py.strip())
PY
}

stage_outcome() {
  local stage="$1"
  local results="$ROOT/build_output/$stage/results.json"
  if [[ ! -f "$results" ]]; then
    echo "missing 1"
    return 0
  fi
  python3 - "$results" <<'PY' 2>/dev/null || { echo "invalid_json 1"; return 0; }
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print("invalid_json 1")
    raise SystemExit(0)

status = str(data.get("status") or "failure")
exit_code = int(data.get("exit_code") or 0)
print(f"{status} {exit_code}")
PY
}

run_stage() {
  local stage="$1"; shift
  local rc=0
  echo "[run_all] ===== stage=$stage ====="
  "$@" || rc=$?
  local outcome
  outcome="$(stage_outcome "$stage")"
  local status exit_code
  read -r status exit_code <<<"$outcome"
  echo "[run_all] stage=$stage script_rc=$rc status=$status exit_code=$exit_code"
  if [[ "$status" == "failure" || "$exit_code" == "1" || "$status" == "missing" || "$status" == "invalid_json" ]]; then
    FAILED_STAGES+=( "$stage" )
  elif [[ "$status" == "skipped" ]]; then
    : # do not record as failure
  fi
}

FAILED_STAGES=()

# Best-effort python selection for pyright stage: prefer explicit override/env; else report python_path.
PYRIGHT_PY="${SCIMLOPSBENCH_PYTHON:-}"
if [[ -z "$PYRIGHT_PY" ]]; then
  PYRIGHT_PY="$(resolve_python_from_report)"
fi

PYRIGHT_ARGS=(--repo "$ROOT" --mode system)
if [[ -n "$PYRIGHT_PY" ]]; then
  PYRIGHT_ARGS=(--repo "$ROOT" --python "$PYRIGHT_PY")
fi

PREP_ARGS=()
if [[ -n "$model_name" ]]; then
  PREP_ARGS+=(--model "$model_name")
fi
if [[ -n "$probe_layers" ]]; then
  PREP_ARGS+=(--probe-layers "$probe_layers")
fi

run_stage "pyright" bash "$ROOT/benchmark_scripts/run_pyright_missing_imports.sh" "${PYRIGHT_ARGS[@]}"
run_stage "prepare" bash "$ROOT/benchmark_scripts/prepare_assets.sh" "${PREP_ARGS[@]}"
run_stage "cpu" bash "$ROOT/benchmark_scripts/run_cpu_entrypoint.sh"
run_stage "cuda" python3 "$ROOT/benchmark_scripts/check_cuda_available.py"
run_stage "single_gpu" bash "$ROOT/benchmark_scripts/run_single_gpu_entrypoint.sh"
run_stage "multi_gpu" bash "$ROOT/benchmark_scripts/run_multi_gpu_entrypoint.sh"
run_stage "env_size" python3 "$ROOT/benchmark_scripts/measure_env_size.py"
run_stage "hallucination" python3 "$ROOT/benchmark_scripts/validate_agent_report.py"
run_stage "summary" python3 "$ROOT/benchmark_scripts/summarize_results.py"

if [[ "${#FAILED_STAGES[@]}" -gt 0 ]]; then
  echo ""
  echo "[run_all] FAILED STAGES (in order): ${FAILED_STAGES[*]}"
  exit 1
fi

echo ""
echo "[run_all] All stages succeeded (or were skipped)."
exit 0

