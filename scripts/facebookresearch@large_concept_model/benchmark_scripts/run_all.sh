#!/usr/bin/env bash
set -u

usage() {
  cat <<'EOF'
Run the full benchmark workflow end-to-end.

Order:
  pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary

Options:
  --report-path <path>     Override report.json path (default: /opt/scimlopsbench/report.json or $SCIMLOPSBENCH_REPORT)
  --python <path>          Override python executable for all stages (highest priority)
EOF
}

report_path=""
python_bin=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 1

report_path="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"

resolve_python_from_report() {
  if [[ -n "${python_bin:-}" ]]; then
    echo "$python_bin"
    return 0
  fi
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    echo "$SCIMLOPSBENCH_PYTHON"
    return 0
  fi
  if [[ -f "$report_path" ]]; then
    python3 - <<PY 2>/dev/null || true
import json
from pathlib import Path
p = Path(${report_path@Q})
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("python_path",""))
except Exception:
    print("")
PY
    return 0
  fi
  echo ""
}

py="$(resolve_python_from_report)"
if [[ -n "$py" ]]; then
  export SCIMLOPSBENCH_PYTHON="$py"
fi

echo "[run_all] repo_root=$repo_root"
echo "[run_all] report_path=$report_path"
echo "[run_all] python=${py:-"(unresolved)"}"

failed_stages=()

stage_outcome() {
  local stage="$1"
  local results_path="$repo_root/build_output/$stage/results.json"
  if [[ ! -f "$results_path" ]]; then
    echo "missing"
    return 0
  fi
  python3 - <<PY 2>/dev/null || true
import json
from pathlib import Path
p = Path(${results_path@Q})
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    status = d.get("status","")
    exit_code = int(d.get("exit_code", 0) or 0)
    if status == "failure" or exit_code == 1:
        print("failure")
    elif status == "skipped":
        print("skipped")
    else:
        print("success")
except Exception:
    print("invalid")
PY
}

run_stage() {
  local stage="$1"
  shift
  echo
  echo "[run_all] ===== stage: $stage ====="
  set +e
  "$@"
  local rc=$?

  local outcome
  outcome="$(stage_outcome "$stage")"
  echo "[run_all] stage=$stage script_rc=$rc outcome=$outcome"

  if [[ "$outcome" == "failure" || "$outcome" == "missing" || "$outcome" == "invalid" ]]; then
    failed_stages+=("$stage")
  fi
}

# Stage 1: pyright
run_stage "pyright" bash benchmark_scripts/run_pyright_missing_imports.sh \
  --repo "$repo_root" \
  --out-root build_output \
  --mode system \
  ${py:+--python "$py"} \
  --install-pyright

# Stage 2: prepare assets
run_stage "prepare" bash benchmark_scripts/prepare_assets.sh \
  ${py:+--python "$py"} \
  --report-path "$report_path" \
  --out-root build_output \
  --assets-root benchmark_assets

# Stage 3: cpu entrypoint
run_stage "cpu" bash benchmark_scripts/run_cpu_entrypoint.sh \
  ${py:+--python "$py"} \
  --report-path "$report_path" \
  --assets-from build_output/prepare/results.json \
  --timeout-sec 600

# Stage 4: cuda check (this stage is expected to exit 1 when CUDA is unavailable)
if [[ -n "$py" ]]; then
  run_stage "cuda" "$py" benchmark_scripts/check_cuda_available.py
else
  run_stage "cuda" python3 benchmark_scripts/check_cuda_available.py
fi

# Stage 5: single GPU
run_stage "single_gpu" bash benchmark_scripts/run_single_gpu_entrypoint.sh \
  ${py:+--python "$py"} \
  --report-path "$report_path" \
  --assets-from build_output/prepare/results.json \
  --timeout-sec 600

# Stage 6: multi GPU
run_stage "multi_gpu" bash benchmark_scripts/run_multi_gpu_entrypoint.sh \
  ${py:+--python "$py"} \
  --report-path "$report_path" \
  --assets-from build_output/prepare/results.json \
  --timeout-sec 1200

# Stage 7: env size
if [[ -n "$py" ]]; then
  run_stage "env_size" "$py" benchmark_scripts/measure_env_size.py --report-path "$report_path"
else
  run_stage "env_size" python3 benchmark_scripts/measure_env_size.py --report-path "$report_path"
fi

# Stage 8: hallucination validation
if [[ -n "$py" ]]; then
  run_stage "hallucination" "$py" benchmark_scripts/validate_agent_report.py --report-path "$report_path"
else
  run_stage "hallucination" python3 benchmark_scripts/validate_agent_report.py --report-path "$report_path"
fi

# Stage 9: summary
if [[ -n "$py" ]]; then
  run_stage "summary" "$py" benchmark_scripts/summarize_results.py
else
  run_stage "summary" python3 benchmark_scripts/summarize_results.py
fi

echo
if [[ "${#failed_stages[@]}" -gt 0 ]]; then
  echo "[run_all] FAILED stages (in order): ${failed_stages[*]}"
  exit 1
fi

echo "[run_all] All stages completed without failures."
exit 0
