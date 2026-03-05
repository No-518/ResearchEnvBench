#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run the full benchmark workflow end-to-end (never aborts early).

Order:
  1) pyright
  2) prepare
  3) cpu
  4) cuda
  5) single_gpu
  6) multi_gpu
  7) env_size
  8) hallucination
  9) summary

Optional:
  --report-path <path>   Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --python <path>        Sets SCIMLOPSBENCH_PYTHON for all stages
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_override="${SCIMLOPSBENCH_PYTHON:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_override="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

export SCIMLOPSBENCH_REPORT="$report_path"
if [[ -n "$python_override" ]]; then
  export SCIMLOPSBENCH_PYTHON="$python_override"
fi

json_py="$(command -v python3 || command -v python || true)"
if [[ -z "$json_py" ]]; then
  echo "python not found on PATH; cannot orchestrate benchmark." >&2
  exit 1
fi

failed_stages=()

stage_outcome() {
  local stage="$1"
  "$json_py" - <<PY
import json
from pathlib import Path
p = Path("build_output") / "$stage" / "results.json"
try:
  d = json.loads(p.read_text(encoding="utf-8"))
  status = d.get("status", "failure")
  exit_code = int(d.get("exit_code", 1))
except Exception:
  status = "failure"
  exit_code = 1
print(f"{status} {exit_code}")
PY
}

run_stage() {
  local stage="$1"; shift
  echo "===== [${stage}] ====="
  echo "+ $*"
  "$@" || true

  local outcome
  outcome="$(stage_outcome "$stage")"
  local st="${outcome%% *}"
  local ec="${outcome##* }"

  if [[ "$st" == "failure" || "$ec" == "1" ]]; then
    failed_stages+=("$stage")
    echo "[run_all] stage=${stage} => FAILED (status=${st}, exit_code=${ec})"
  elif [[ "$st" == "skipped" ]]; then
    echo "[run_all] stage=${stage} => SKIPPED"
  else
    echo "[run_all] stage=${stage} => OK"
  fi
  echo
}

run_stage pyright bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root"
run_stage prepare bash benchmark_scripts/prepare_assets.sh --repo "$repo_root" --report-path "$report_path" ${python_override:+--python "$python_override"}
run_stage cpu bash benchmark_scripts/run_cpu_entrypoint.sh --repo "$repo_root" --report-path "$report_path" ${python_override:+--python "$python_override"}
run_stage cuda "$json_py" benchmark_scripts/check_cuda_available.py --report-path "$report_path" ${python_override:+--python "$python_override"}
run_stage single_gpu bash benchmark_scripts/run_single_gpu_entrypoint.sh --repo "$repo_root" --report-path "$report_path" ${python_override:+--python "$python_override"}
run_stage multi_gpu bash benchmark_scripts/run_multi_gpu_entrypoint.sh --repo "$repo_root" --report-path "$report_path" ${python_override:+--python "$python_override"}
run_stage env_size "$json_py" benchmark_scripts/measure_env_size.py --report-path "$report_path"
run_stage hallucination "$json_py" benchmark_scripts/validate_agent_report.py --report-path "$report_path"
run_stage summary "$json_py" benchmark_scripts/summarize_results.py

if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "[run_all] FAILED stages (in order): ${failed_stages[*]}"
  exit 1
fi
echo "[run_all] All stages succeeded (or were skipped)."
exit 0

