#!/usr/bin/env bash
set -u
set -o pipefail

usage() {
  cat <<'EOF'
Run the full benchmark workflow end-to-end (does not abort early on failures).

Order:
  pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary

Optional:
  --python <path>        Explicit python executable to use for python-based stages (highest priority for those stages)
  --report-path <path>   Override /opt/scimlopsbench/report.json
  -h|--help              Show help

Examples:
  bash benchmark_scripts/run_all.sh
  bash benchmark_scripts/run_all.sh --report-path /opt/scimlopsbench/report.json
  bash benchmark_scripts/run_all.sh --python /opt/scimlopsbench/python
EOF
}

python_bin=""
report_path=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

BOOTSTRAP_PY="$(command -v python >/dev/null 2>&1 && echo python || echo python3)"

common_args=()
[[ -n "$python_bin" ]] && common_args+=(--python "$python_bin")
[[ -n "$report_path" ]] && common_args+=(--report-path "$report_path")

failed_stages=()

stage_results_ok() {
  local stage="$1"
  local results_path="build_output/${stage}/results.json"
  if [[ ! -s "$results_path" ]]; then
    echo "[run_all] stage=${stage} results missing: ${results_path}"
    return 1
  fi
  "$BOOTSTRAP_PY" - <<PY "$results_path"
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    sys.exit(1)

status = str(data.get("status", ""))
try:
    exit_code = int(data.get("exit_code", 1))
except Exception:
    exit_code = 1

if status == "skipped":
    sys.exit(0)
if status == "failure" or exit_code == 1:
    sys.exit(1)
sys.exit(0)
PY
}

run_stage() {
  local stage="$1"; shift
  echo "========== [run_all] stage=${stage} =========="
  "$@" || true
  if ! stage_results_ok "$stage"; then
    failed_stages+=("$stage")
    echo "[run_all] stage=${stage} => FAILED"
  else
    status="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.load(open(sys.argv[1],"r",encoding="utf-8")).get("status",""))' "build_output/${stage}/results.json" 2>/dev/null || echo "")"
    echo "[run_all] stage=${stage} => ${status:-unknown}"
  fi
}

run_stage "pyright" bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root" "${common_args[@]}"
run_stage "prepare" bash benchmark_scripts/prepare_assets.sh "${common_args[@]}"
run_stage "cpu" bash benchmark_scripts/run_cpu_entrypoint.sh "${common_args[@]}"
run_stage "cuda" "$BOOTSTRAP_PY" benchmark_scripts/check_cuda_available.py "${common_args[@]}"
run_stage "single_gpu" bash benchmark_scripts/run_single_gpu_entrypoint.sh "${common_args[@]}"
run_stage "multi_gpu" bash benchmark_scripts/run_multi_gpu_entrypoint.sh "${common_args[@]}"
run_stage "env_size" "$BOOTSTRAP_PY" benchmark_scripts/measure_env_size.py ${report_path:+--report-path "$report_path"}
run_stage "hallucination" "$BOOTSTRAP_PY" benchmark_scripts/validate_agent_report.py ${report_path:+--report-path "$report_path"}
run_stage "summary" "$BOOTSTRAP_PY" benchmark_scripts/summarize_results.py

if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "========== [run_all] FINAL: FAILED =========="
  echo "[run_all] failed_stages: ${failed_stages[*]}"
  exit 1
fi

echo "========== [run_all] FINAL: SUCCESS =========="
exit 0

