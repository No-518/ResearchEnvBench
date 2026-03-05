#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run the full benchmark workflow (no early aborts).

Order:
  pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary

Optional:
  --report-path <path>            Override report.json path for stages that read it
  --python <path>                 Override python executable for stages that accept it
  --model-id <repo_id>            Passed to prepare_assets.sh
  --model-revision <rev>          Passed to prepare_assets.sh
  --prompt <text>                 Passed to prepare_assets.sh
  --cuda-visible-devices <list>   Passed to run_multi_gpu_entrypoint.sh (default: 0,1)
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

report_path=""
python_bin=""
model_id=""
model_revision=""
prompt=""
cuda_visible_devices=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --python)
      python_bin="${2:-}"; shift 2 ;;
    --model-id)
      model_id="${2:-}"; shift 2 ;;
    --model-revision)
      model_revision="${2:-}"; shift 2 ;;
    --prompt)
      prompt="${2:-}"; shift 2 ;;
    --cuda-visible-devices)
      cuda_visible_devices="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

py_exec=""
if command -v python3 >/dev/null 2>&1; then
  py_exec="python3"
elif command -v python >/dev/null 2>&1; then
  py_exec="python"
else
  echo "python/python3 not found in PATH" >&2
  exit 1
fi

json_get() {
  local file="$1"
  local key="$2"
  "$py_exec" - "$file" "$key" <<'PY' 2>/dev/null || true
import json, sys
path = sys.argv[1]
key = sys.argv[2]
try:
  data = json.load(open(path, encoding="utf-8"))
except Exception:
  sys.exit(0)
val = data.get(key, "")
if isinstance(val, (dict, list)):
  print("")
else:
  print(val)
PY
}

failed_stages=()

run_stage() {
  local stage="$1"
  shift
  echo "===== [run_all] stage=$stage ====="
  "$@" || true

  local results_path="build_output/${stage}/results.json"
  if [[ ! -f "$results_path" ]]; then
    echo "[run_all] stage=$stage results.json missing: $results_path"
    failed_stages+=("$stage")
    return
  fi

  local status exit_code
  status="$(json_get "$results_path" "status")"
  exit_code="$(json_get "$results_path" "exit_code")"
  [[ -z "$exit_code" ]] && exit_code="1"

  if [[ "$status" == "skipped" ]]; then
    echo "[run_all] stage=$stage status=skipped"
    return
  fi
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    echo "[run_all] stage=$stage status=failure"
    failed_stages+=("$stage")
    return
  fi
  echo "[run_all] stage=$stage status=success"
}

common_report_args=()
[[ -n "$report_path" ]] && common_report_args+=(--report-path "$report_path")

common_python_args=()
[[ -n "$python_bin" ]] && common_python_args+=(--python "$python_bin")

prepare_args=()
[[ -n "$model_id" ]] && prepare_args+=(--model-id "$model_id")
[[ -n "$model_revision" ]] && prepare_args+=(--model-revision "$model_revision")
[[ -n "$prompt" ]] && prepare_args+=(--prompt "$prompt")

multi_args=()
[[ -n "$cuda_visible_devices" ]] && multi_args+=(--cuda-visible-devices "$cuda_visible_devices")

run_stage "pyright" bash benchmark_scripts/run_pyright_missing_imports.sh --repo "$repo_root" "${common_report_args[@]}" "${common_python_args[@]}"
run_stage "prepare" bash benchmark_scripts/prepare_assets.sh "${common_report_args[@]}" "${common_python_args[@]}" "${prepare_args[@]}"
run_stage "cpu" bash benchmark_scripts/run_cpu_entrypoint.sh "${common_report_args[@]}" "${common_python_args[@]}"
run_stage "cuda" "$py_exec" benchmark_scripts/check_cuda_available.py "${common_report_args[@]}"
run_stage "single_gpu" bash benchmark_scripts/run_single_gpu_entrypoint.sh "${common_report_args[@]}" "${common_python_args[@]}"
run_stage "multi_gpu" bash benchmark_scripts/run_multi_gpu_entrypoint.sh "${common_report_args[@]}" "${common_python_args[@]}" "${multi_args[@]}"
run_stage "env_size" "$py_exec" benchmark_scripts/measure_env_size.py "${common_report_args[@]}"
run_stage "hallucination" "$py_exec" benchmark_scripts/validate_agent_report.py "${common_report_args[@]}"
run_stage "summary" "$py_exec" benchmark_scripts/summarize_results.py

echo "===== [run_all] done ====="
if [[ "${#failed_stages[@]}" -gt 0 ]]; then
  echo "[run_all] failed stages (in order): ${failed_stages[*]}"
  exit 1
fi
echo "[run_all] all stages succeeded (skipped not counted as failure)"
exit 0

