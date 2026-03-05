#!/usr/bin/env bash
set -u
set -o pipefail

usage() {
  cat <<'EOF'
Run the full reproducible benchmark chain (no early abort):
  pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary

Options:
  --report-path <path>   Override report path (default: /opt/scimlopsbench/report.json)
  --python <path>        Override python executable used for stages (default: from report.json)
EOF
}

report_path=""
python_bin=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

failed_stages=()

resolve_python_from_report() {
  local rp="${1:-}"
  python3 - <<'PY' 2>/dev/null || true
import json, os, pathlib
rp = os.environ.get("RP") or os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"
p = pathlib.Path(rp)
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print(data.get("python_path","") or "")
except Exception:
    print("")
PY
}

stage_outcome() {
  local stage="$1"
  local path="build_output/${stage}/results.json"
  if [[ ! -f "$path" ]]; then
    echo "failure 1"
    return 0
  fi
  python3 - "$path" <<'PY' 2>/dev/null || true
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    status = str(data.get("status","failure"))
    exit_code = int(data.get("exit_code", 1))
    print(status, exit_code)
except Exception:
    print("failure 1")
PY
}

if [[ -z "$python_bin" ]]; then
  python_bin="$(RP="$report_path" resolve_python_from_report "$report_path")"
fi

echo "[run_all] repo_root=$repo_root"
echo "[run_all] report_path=${report_path:-/opt/scimlopsbench/report.json}"
echo "[run_all] resolved_python=${python_bin:-<none>}"

run_stage() {
  local stage="$1"; shift
  echo
  echo "========== STAGE: $stage =========="
  set +e
  "$@"
  local rc="$?"
  set -e

  local outcome
  outcome="$(stage_outcome "$stage")"
  local status exit_code
  status="$(echo "$outcome" | awk '{print $1}')"
  exit_code="$(echo "$outcome" | awk '{print $2}')"

  echo "[run_all] stage=$stage rc=$rc status=$status exit_code=$exit_code"
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    failed_stages+=("$stage")
  fi
}

set -e

# 1) Pyright
if [[ -n "$python_bin" ]]; then
  run_stage "pyright" bash benchmark_scripts/run_pyright_missing_imports.sh --python "$python_bin" --repo "$repo_root"
else
  run_stage "pyright" bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root"
fi

# 2) Prepare assets
prepare_cmd=(bash benchmark_scripts/prepare_assets.sh)
[[ -n "$python_bin" ]] && prepare_cmd+=(--python "$python_bin")
[[ -n "$report_path" ]] && prepare_cmd+=(--report-path "$report_path")
run_stage "prepare" "${prepare_cmd[@]}"

# 3) CPU run
cpu_cmd=(bash benchmark_scripts/run_cpu_entrypoint.sh)
[[ -n "$python_bin" ]] && cpu_cmd+=(--python "$python_bin")
[[ -n "$report_path" ]] && cpu_cmd+=(--report-path "$report_path")
run_stage "cpu" "${cpu_cmd[@]}"

# 4) CUDA check (use report python if available)
if [[ -n "$python_bin" ]]; then
  run_stage "cuda" "$python_bin" benchmark_scripts/check_cuda_available.py
else
  run_stage "cuda" python3 benchmark_scripts/check_cuda_available.py
fi

# 5) Single GPU run
single_cmd=(bash benchmark_scripts/run_single_gpu_entrypoint.sh)
[[ -n "$python_bin" ]] && single_cmd+=(--python "$python_bin")
[[ -n "$report_path" ]] && single_cmd+=(--report-path "$report_path")
run_stage "single_gpu" "${single_cmd[@]}"

# 6) Multi GPU run
multi_cmd=(bash benchmark_scripts/run_multi_gpu_entrypoint.sh)
[[ -n "$python_bin" ]] && multi_cmd+=(--python "$python_bin")
[[ -n "$report_path" ]] && multi_cmd+=(--report-path "$report_path")
run_stage "multi_gpu" "${multi_cmd[@]}"

# 7) Environment size
env_size_cmd=(python3 benchmark_scripts/measure_env_size.py)
[[ -n "$report_path" ]] && env_size_cmd+=(--report-path "$report_path")
run_stage "env_size" "${env_size_cmd[@]}"

# 8) Hallucination validation
hall_cmd=(python3 benchmark_scripts/validate_agent_report.py)
[[ -n "$report_path" ]] && hall_cmd+=(--report-path "$report_path")
run_stage "hallucination" "${hall_cmd[@]}"

# 9) Summary
run_stage "summary" python3 benchmark_scripts/summarize_results.py

echo
echo "========== FINAL =========="
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "Failed stages (in order): ${failed_stages[*]}"
  exit 1
fi
echo "All stages succeeded (or were skipped)."
exit 0
