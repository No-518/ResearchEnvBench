#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU (>=2 GPUs) execution using the repository's recommended distributed entrypoint.

This repository (city2graph) appears to be a library without a train/infer CLI entrypoint.
By default this stage will mark itself as skipped unless an explicit repo-native distributed command
is provided via SCIMLOPSBENCH_MULTI_GPU_COMMAND.

Environment variables:
  SCIMLOPSBENCH_MULTI_GPU_COMMAND    Repo-native distributed command to run. You may use "{python}" placeholder
                                    which will be replaced with python_path from report.json.
  SCIMLOPSBENCH_MULTI_GPU_DEVICES    Default: "0,1" (sets CUDA_VISIBLE_DEVICES)

Optional:
  --repo <path>                      Repo root (default: auto = parent of benchmark_scripts)
EOF
}

repo=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_DEFAULT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="${repo:-$REPO_ROOT_DEFAULT}"

STAGE_DIR="$REPO_ROOT/build_output/multi_gpu"
LOG_PATH="$STAGE_DIR/log.txt"
RESULTS_JSON="$STAGE_DIR/results.json"

mkdir -p "$STAGE_DIR"
: >"$LOG_PATH"
exec > >(tee -a "$LOG_PATH") 2>&1

cd "$REPO_ROOT"

timeout_sec=1200
framework="unknown"
task="train"
status="skipped"
exit_code=0
skip_reason="repo_not_supported"
failure_category="unknown"
decision_reason=""
command_str="skipped"

git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
sys_py="$(command -v python3 || command -v python || true)"

# Load dataset/model info from prepare stage if available (best-effort).
dataset_path="$REPO_ROOT/benchmark_assets/dataset"
dataset_source=""
dataset_version=""
dataset_sha256=""
model_path="$REPO_ROOT/benchmark_assets/model"
model_source=""
model_version=""
model_sha256=""
prep_results="$REPO_ROOT/build_output/prepare/results.json"
if [[ -n "$sys_py" && -f "$prep_results" ]]; then
  mapfile -t _prep_vals < <("$sys_py" - <<PY 2>/dev/null || true
import json

def s(v):
    return v.strip() if isinstance(v, str) else ""

try:
    d = json.load(open("$prep_results","r",encoding="utf-8"))
except Exception:
    d = {}

ds = (d.get("assets") or {}).get("dataset") or {}
md = (d.get("assets") or {}).get("model") or {}
for obj in (ds, md):
    for k in ("path", "source", "version", "sha256"):
        print(s(obj.get(k, "")))
PY
)
  dataset_path="${_prep_vals[0]:-}"
  dataset_source="${_prep_vals[1]:-}"
  dataset_version="${_prep_vals[2]:-}"
  dataset_sha256="${_prep_vals[3]:-}"
  model_path="${_prep_vals[4]:-}"
  model_source="${_prep_vals[5]:-}"
  model_version="${_prep_vals[6]:-}"
  model_sha256="${_prep_vals[7]:-}"
  [[ -z "$dataset_path" ]] && dataset_path="$REPO_ROOT/benchmark_assets/dataset"
  [[ -z "$model_path" ]] && model_path="$REPO_ROOT/benchmark_assets/model"
fi

# Resolve python_path from report.json for optional {python} substitution and GPU detection.
python_exe=""
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
if [[ -n "$sys_py" && -f "$report_path" ]]; then
  python_exe="$("$sys_py" - <<PY 2>/dev/null || true
import json, os
p = os.environ.get("SCIMLOPSBENCH_REPORT", "/opt/scimlopsbench/report.json")
try:
    data = json.load(open(p, "r", encoding="utf-8"))
    val = data.get("python_path")
    if isinstance(val, str) and val.strip():
        print(val.strip())
except Exception:
    pass
PY
)"
fi

multi_cmd="${SCIMLOPSBENCH_MULTI_GPU_COMMAND:-}"
gpu_devs="${SCIMLOPSBENCH_MULTI_GPU_DEVICES:-0,1}"

gpu_count=0

if [[ -z "$multi_cmd" ]]; then
  decision_reason="No repo-native multi-GPU distributed entrypoint detected (no CLI scripts/console entrypoints found); set SCIMLOPSBENCH_MULTI_GPU_COMMAND to run a repo-native command."
  echo "$decision_reason"
else
  # Hardware pre-check: only needed when actually attempting multi-GPU.
  gpu_count=0
  if [[ -n "$python_exe" ]]; then
    set +e
    gpu_count="$("$python_exe" - <<'PY' 2>/dev/null
try:
    import torch
    print(int(torch.cuda.device_count() if torch.cuda.is_available() else 0))
except Exception:
    print(0)
PY
)"
    set -e
  fi
  if [[ -z "${gpu_count:-}" || "$gpu_count" == "0" ]]; then
    if command -v nvidia-smi >/dev/null 2>&1; then
      set +e
      gpu_count="$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ' | tr -d ' ')"
      set -e
    fi
  fi
  gpu_count="${gpu_count:-0}"

  # Hardware pre-check.
  if [[ "${gpu_count:-0}" -lt 2 ]]; then
    status="failure"
    exit_code=1
    skip_reason="insufficient_hardware"
    failure_category="unknown"
    decision_reason="Multi-GPU requires >=2 GPUs; detected gpu_count=${gpu_count}."
  else
    status="failure"
    exit_code=1
    skip_reason="unknown"
    failure_category="runtime"
    decision_reason="Using SCIMLOPSBENCH_MULTI_GPU_COMMAND override."

    cmd_expanded="$multi_cmd"
    if [[ -n "$python_exe" ]]; then
      cmd_expanded="${cmd_expanded//\{python\}/$python_exe}"
    fi
    command_str="$cmd_expanded"

    echo "Running multi-GPU command with CUDA_VISIBLE_DEVICES=$gpu_devs (gpu_count=${gpu_count}):"
    echo "  $command_str"

    export CUDA_VISIBLE_DEVICES="$gpu_devs"

    set +e
    timeout "$timeout_sec" bash -lc "$command_str"
    rc=$?
    set -e

    if [[ $rc -eq 0 ]]; then
      status="success"
      exit_code=0
      failure_category="unknown"
    else
      status="failure"
      exit_code=1
      if [[ $rc -eq 124 ]]; then
        failure_category="timeout"
      fi
    fi
  fi
fi

error_excerpt="$(tail -n 240 "$LOG_PATH" 2>/dev/null | tail -n 220 || true)"

if [[ -z "$sys_py" ]]; then
  cat >"$RESULTS_JSON" <<JSON
{
  "status": "$status",
  "skip_reason": "$skip_reason",
  "exit_code": $exit_code,
  "stage": "multi_gpu",
  "task": "$task",
  "command": "$command_str",
  "timeout_sec": $timeout_sec,
  "framework": "$framework",
  "assets": {
    "dataset": {"path": "$dataset_path", "source": "$dataset_source", "version": "$dataset_version", "sha256": "$dataset_sha256"},
    "model": {"path": "$model_path", "source": "$model_source", "version": "$model_version", "sha256": "$model_sha256"}
  },
  "meta": {
    "python": "$python_exe",
    "git_commit": "$git_commit",
    "env_vars": {},
    "decision_reason": "$decision_reason",
    "gpu_count": $gpu_count
  },
  "failure_category": "$failure_category",
  "error_excerpt": ""
}
JSON
else
  export status skip_reason exit_code timeout_sec framework task command_str decision_reason git_commit python_exe error_excerpt RESULTS_JSON gpu_count failure_category
  export dataset_path dataset_source dataset_version dataset_sha256
  export model_path model_source model_version model_sha256
  "$sys_py" - <<'PY'
import json
import os

def env_snapshot() -> dict:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "PATH",
        "PYTHONPATH",
    ]
    return {k: os.environ.get(k, "") for k in keys if k in os.environ}

payload = {
    "status": os.environ["status"],
    "skip_reason": os.environ.get("skip_reason", "unknown"),
    "exit_code": int(os.environ["exit_code"]),
    "stage": "multi_gpu",
    "task": os.environ.get("task", "train"),
    "command": os.environ.get("command_str", ""),
    "timeout_sec": int(os.environ.get("timeout_sec", "1200")),
    "framework": os.environ.get("framework", "unknown"),
    "assets": {
        "dataset": {
            "path": os.environ.get("dataset_path", ""),
            "source": os.environ.get("dataset_source", ""),
            "version": os.environ.get("dataset_version", ""),
            "sha256": os.environ.get("dataset_sha256", ""),
        },
        "model": {
            "path": os.environ.get("model_path", ""),
            "source": os.environ.get("model_source", ""),
            "version": os.environ.get("model_version", ""),
            "sha256": os.environ.get("model_sha256", ""),
        },
    },
    "meta": {
        "python": os.environ.get("python_exe", ""),
        "git_commit": os.environ.get("git_commit", ""),
        "env_vars": env_snapshot(),
        "decision_reason": os.environ.get("decision_reason", ""),
        "gpu_count": int(os.environ.get("gpu_count", "0") or "0"),
    },
    "failure_category": os.environ.get("failure_category", "unknown"),
    "error_excerpt": os.environ.get("error_excerpt", "")[-8000:],
}

with open(os.environ["RESULTS_JSON"], "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, ensure_ascii=False)
PY
fi

exit "$exit_code"
