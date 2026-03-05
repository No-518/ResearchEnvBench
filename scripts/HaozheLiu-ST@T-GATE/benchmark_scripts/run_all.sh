#!/usr/bin/env bash
set -u
set -o pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_override=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_override="${2:-}"; shift 2 ;;
    -h|--help)
      cat <<'EOF'
Run the full benchmark workflow in order, without aborting early on failures.

Usage:
  bash benchmark_scripts/run_all.sh [--report-path /path/to/report.json] [--python /path/to/python]
EOF
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

bootstrap_py="$(command -v python3 || command -v python || true)"
if [[ -z "$bootstrap_py" ]]; then
  echo "[run_all] python3/python not found in PATH; cannot parse stage results." >&2
fi

failed_stages=()

read_stage_outcome() {
  local stage="$1"
  local rp="$repo_root/build_output/$stage/results.json"
  if [[ ! -f "$rp" || -z "$bootstrap_py" ]]; then
    echo "failure 1"
    return
  fi
  "$bootstrap_py" - <<PY 2>/dev/null || echo "failure 1"
import json
from pathlib import Path
p=Path("$rp")
try:
  d=json.loads(p.read_text(encoding="utf-8"))
  st=str(d.get("status","failure"))
  raw=d.get("exit_code",1)
  try:
    ec=int(raw) if raw is not None else 1
  except Exception:
    ec=1
  print(st, ec)
except Exception:
  print("failure", 1)
PY
}

run_stage() {
  local stage="$1"; shift
  echo "========== $stage =========="
  "$@" || true

  local outcome
  outcome="$(read_stage_outcome "$stage")"
  local st ec
  st="$(awk '{print $1}' <<<"$outcome")"
  ec="$(awk '{print $2}' <<<"$outcome")"
  echo "[run_all] stage=$stage status=$st exit_code=$ec"

  if [[ "$st" == "failure" || "$ec" -eq 1 ]]; then
    failed_stages+=("$stage")
  fi
}

common_args=(
  --report-path "$report_path"
)
if [[ -n "$python_override" ]]; then
  common_args+=(--python "$python_override")
fi

run_stage pyright bash "$repo_root/benchmark_scripts/run_pyright_missing_imports.sh" --repo "$repo_root" --report-path "$report_path" ${python_override:+--python "$python_override"}
run_stage prepare bash "$repo_root/benchmark_scripts/prepare_assets.sh" --report-path "$report_path" ${python_override:+--python "$python_override"}
run_stage cpu bash "$repo_root/benchmark_scripts/run_cpu_entrypoint.sh" "${common_args[@]}"
run_stage cuda "$bootstrap_py" "$repo_root/benchmark_scripts/check_cuda_available.py" --report-path "$report_path"
run_stage single_gpu bash "$repo_root/benchmark_scripts/run_single_gpu_entrypoint.sh" "${common_args[@]}"
run_stage multi_gpu bash "$repo_root/benchmark_scripts/run_multi_gpu_entrypoint.sh" --report-path "$report_path"
run_stage env_size "$bootstrap_py" "$repo_root/benchmark_scripts/measure_env_size.py" --report-path "$report_path"
run_stage hallucination "$bootstrap_py" "$repo_root/benchmark_scripts/validate_agent_report.py" --report-path "$report_path"
run_stage summary "$bootstrap_py" "$repo_root/benchmark_scripts/summarize_results.py"

echo "========== FINAL =========="
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "[run_all] Failed stages (in order): ${failed_stages[*]}"
  exit 1
fi
echo "[run_all] All stages succeeded (skipped stages do not count as failure)."
exit 0
