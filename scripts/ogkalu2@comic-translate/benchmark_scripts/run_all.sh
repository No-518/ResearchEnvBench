#!/usr/bin/env bash
set -u
set -o pipefail

usage() {
  cat <<'EOF'
Run the full benchmark workflow end-to-end (never aborts early).

Order:
  pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary

Options:
  --report-path <path>   Override agent report path (default: /opt/scimlopsbench/report.json)
  --python <path>        Override python interpreter used for python-based stages
EOF
}

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

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

export SCIMLOPSBENCH_REPORT="$report_path"
if [[ -n "$python_override" ]]; then
  export SCIMLOPSBENCH_PYTHON="$python_override"
fi
# Prevent creating __pycache__ in the repo or environment.
export PYTHONDONTWRITEBYTECODE=1

resolve_python() {
  if [[ -n "${python_override}" ]]; then
    echo "$python_override"
    return 0
  fi
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    echo "${SCIMLOPSBENCH_PYTHON}"
    return 0
  fi
  if [[ -f "$report_path" ]]; then
    python - "$report_path" <<'PY' 2>/dev/null || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    v = data.get("python_path", "")
    print(v if isinstance(v, str) else "")
except Exception:
    print("")
PY
    return 0
  fi
  echo "python"
  return 0
}

PY_BIN="$(resolve_python)"
if [[ -z "$PY_BIN" ]]; then
  PY_BIN="python"
fi

failures=()

stage_outcome() {
  local stage="$1"
  local results="$REPO_ROOT/build_output/$stage/results.json"
  if [[ ! -f "$results" ]]; then
    echo "stage=$stage outcome=failure (missing results.json)"
    failures+=("$stage")
    return 0
  fi
  local parsed
  parsed="$("$PY_BIN" - "$results" <<'PY' 2>/dev/null || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    status = d.get("status", "failure")
    raw_exit = d.get("exit_code", 1)
    try:
        exit_code = int(raw_exit)
    except Exception:
        exit_code = 1
    print(f"{status} {exit_code}")
except Exception:
    print("failure 1")
PY
)"
  local status exit_code
  status="$(awk '{print $1}' <<<"$parsed")"
  exit_code="$(awk '{print $2}' <<<"$parsed")"
  if [[ "$status" == "skipped" ]]; then
    echo "stage=$stage outcome=skipped"
    return 0
  fi
  if [[ "$status" == "failure" || "$exit_code" == "1" ]]; then
    echo "stage=$stage outcome=failure"
    failures+=("$stage")
    return 0
  fi
  echo "stage=$stage outcome=success"
  return 0
}

echo "repo=$REPO_ROOT"
echo "report_path=$report_path"
echo "python_for_py_stages=$PY_BIN"

echo "== Stage: pyright =="
bash "$REPO_ROOT/benchmark_scripts/run_pyright_missing_imports.sh" --repo "$REPO_ROOT" --report-path "$report_path" ${python_override:+--python "$python_override"} || true
stage_outcome "pyright"

echo "== Stage: prepare =="
bash "$REPO_ROOT/benchmark_scripts/prepare_assets.sh" --repo "$REPO_ROOT" --report-path "$report_path" ${python_override:+--python "$python_override"} || true
stage_outcome "prepare"

echo "== Stage: cpu =="
bash "$REPO_ROOT/benchmark_scripts/run_cpu_entrypoint.sh" --repo "$REPO_ROOT" --report-path "$report_path" ${python_override:+--python "$python_override"} || true
stage_outcome "cpu"

echo "== Stage: cuda =="
"$PY_BIN" "$REPO_ROOT/benchmark_scripts/check_cuda_available.py" || true
stage_outcome "cuda"

echo "== Stage: single_gpu =="
bash "$REPO_ROOT/benchmark_scripts/run_single_gpu_entrypoint.sh" --repo "$REPO_ROOT" --report-path "$report_path" ${python_override:+--python "$python_override"} || true
stage_outcome "single_gpu"

echo "== Stage: multi_gpu =="
bash "$REPO_ROOT/benchmark_scripts/run_multi_gpu_entrypoint.sh" --repo "$REPO_ROOT" --report-path "$report_path" ${python_override:+--python "$python_override"} || true
stage_outcome "multi_gpu"

echo "== Stage: env_size =="
"$PY_BIN" "$REPO_ROOT/benchmark_scripts/measure_env_size.py" --report-path "$report_path" || true
stage_outcome "env_size"

echo "== Stage: hallucination =="
"$PY_BIN" "$REPO_ROOT/benchmark_scripts/validate_agent_report.py" --report-path "$report_path" || true
stage_outcome "hallucination"

echo "== Stage: summary =="
"$PY_BIN" "$REPO_ROOT/benchmark_scripts/summarize_results.py" || true
stage_outcome "summary"

if [[ "${#failures[@]}" -gt 0 ]]; then
  echo "FAILED STAGES (in order): ${failures[*]}"
  exit 1
fi
echo "All stages succeeded (or skipped)."
exit 0
