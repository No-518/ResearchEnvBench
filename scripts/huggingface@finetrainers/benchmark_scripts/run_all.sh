#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run the full benchmark workflow end-to-end (no early abort on failures).

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

Options:
  --out-dir <path>       Root output dir (default: build_output)
  --report-path <path>   Agent report JSON (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  --python <path>        Explicit python to use for runner-resolved stages (overrides report)
EOF
}

out_root="build_output"
report_path=""
python_override=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out-dir)
      out_root="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --python)
      python_override="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

sys_python="$(command -v python3 || command -v python || true)"

common_args=()
report_only_args=()
[[ -n "$report_path" ]] && common_args+=(--report-path "$report_path") && report_only_args+=(--report-path "$report_path")
[[ -n "$python_override" ]] && common_args+=(--python "$python_override")

stage_failed=()

read_stage_outcome() {
  local stage="$1"
  local results_path="$repo_root/$out_root/$stage/results.json"
  if [[ -z "$sys_python" ]]; then
    echo "unknown 1"
    return 0
  fi
  if [[ ! -f "$results_path" ]]; then
    echo "missing 1"
    return 0
  fi
  "$sys_python" - <<PY 2>/dev/null || echo "invalid 1"
import json
from pathlib import Path
p=Path(${results_path@Q})
try:
  d=json.loads(p.read_text())
  status=str(d.get("status") or "failure")
  exit_code=int(d.get("exit_code") or 0)
  print(status, exit_code)
except Exception:
  print("invalid", 1)
PY
}

run_stage() {
  local stage="$1"
  shift
  echo ""
  echo "==================== stage=$stage ===================="
  set +e
  "$@"
  local cmd_ec=$?
  set -e

  read -r status exit_code < <(read_stage_outcome "$stage")

  if [[ "$status" == "skipped" ]]; then
    echo "[run_all] stage=$stage skipped (cmd_ec=$cmd_ec)"
    return 0
  fi
  if [[ "$status" == "failure" || "$exit_code" -eq 1 ]]; then
    echo "[run_all] stage=$stage FAILED (status=$status exit_code=$exit_code cmd_ec=$cmd_ec)"
    stage_failed+=("$stage")
  else
    echo "[run_all] stage=$stage OK (status=$status exit_code=$exit_code cmd_ec=$cmd_ec)"
  fi
  return 0
}

run_stage "pyright" bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root" --out-dir "$out_root" "${common_args[@]}"
run_stage "prepare" bash benchmark_scripts/prepare_assets.sh --out-dir "$out_root" "${common_args[@]}"
run_stage "cpu" bash benchmark_scripts/run_cpu_entrypoint.sh --out-dir "$out_root" "${common_args[@]}"
run_stage "cuda" "$sys_python" benchmark_scripts/check_cuda_available.py --out-root "$out_root" "${report_only_args[@]}"
run_stage "single_gpu" bash benchmark_scripts/run_single_gpu_entrypoint.sh --out-dir "$out_root" "${common_args[@]}"
run_stage "multi_gpu" bash benchmark_scripts/run_multi_gpu_entrypoint.sh --out-dir "$out_root" "${common_args[@]}"
run_stage "env_size" "$sys_python" benchmark_scripts/measure_env_size.py --out-root "$out_root" "${report_only_args[@]}"
run_stage "hallucination" "$sys_python" benchmark_scripts/validate_agent_report.py --out-root "$out_root" "${report_only_args[@]}"
run_stage "summary" "$sys_python" benchmark_scripts/summarize_results.py --out-root "$out_root"

echo ""
echo "==================== run_all summary ===================="
if [[ "${#stage_failed[@]}" -gt 0 ]]; then
  echo "FAILED stages (in order): ${stage_failed[*]}"
  exit 1
fi
echo "All stages succeeded (or skipped)."
exit 0
