#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU distributed run via repository entrypoint, when supported.

This repository is a FastAPI inference server and does not expose a distributed/multi-GPU launch
path (DDP/torchrun/accelerate) for inference/training. This stage therefore auto-skips with a
reviewable reason when multi-GPU is not applicable or not supported.

Outputs (always written):
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Options:
  --python <cmd>       Python command or path to use (highest priority)
  --repo <path>        Repo root (default: auto-detect)
  --devices <csv>      CUDA_VISIBLE_DEVICES to use (default: 0,1)
EOF
}

python_arg=""
repo=""
devices="0,1"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_arg="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    --devices) devices="${2:-0,1}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
repo="${repo:-$REPO_ROOT}"

OUT_DIR="$REPO_ROOT/build_output/multi_gpu"
mkdir -p "$OUT_DIR"
LOG_FILE="$OUT_DIR/log.txt"
RESULTS_JSON="$OUT_DIR/results.json"

exec > >(tee "$LOG_FILE") 2>&1

cd "$repo"

stage_status="success"
failure_category=""
skip_reason="unknown"
exit_code=0
timeout_sec=1200
framework="pytorch"

# Avoid writing __pycache__ into the repository.
export PYTHONDONTWRITEBYTECODE=1

PY_CMD=(python)
PY_SOURCE=""

resolve_python() {
  if [[ -n "$python_arg" ]]; then
    # shellcheck disable=SC2206
    PY_CMD=($python_arg)
    PY_SOURCE="cli"
    return 0
  fi
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    # shellcheck disable=SC2206
    PY_CMD=(${SCIMLOPSBENCH_PYTHON})
    PY_SOURCE="env"
    return 0
  fi

  local report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
  if [[ ! -f "$report_path" ]]; then
    PY_SOURCE="missing_report"
    return 1
  fi

  local sys_py
  if command -v python3 >/dev/null 2>&1; then
    sys_py="python3"
  elif command -v python >/dev/null 2>&1; then
    sys_py="python"
  else
    PY_SOURCE="missing_report"
    return 1
  fi

  local resolved
  resolved="$("$sys_py" - <<'PY'
import json, os
path = os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"
data = json.load(open(path, "r", encoding="utf-8"))
print(data.get("python_path",""))
PY
)" || return 1

  if [[ -z "$resolved" ]]; then
    PY_SOURCE="missing_report"
    return 1
  fi

  # shellcheck disable=SC2206
  PY_CMD=($resolved)
  PY_SOURCE="report"
  return 0
}

if ! resolve_python; then
  echo "Failed to resolve python (missing report and no --python/SCIMLOPSBENCH_PYTHON)." >&2
  stage_status="failure"
  failure_category="missing_report"
  exit_code=1
fi

python_cmd_str="${PY_CMD[*]}"
echo "Repo: $repo"
echo "Python cmd: $python_cmd_str (source=$PY_SOURCE)"

WRITER_PY_CMD=("${PY_CMD[@]}")
if ! "${WRITER_PY_CMD[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    WRITER_PY_CMD=(python3)
  elif command -v python >/dev/null 2>&1; then
    WRITER_PY_CMD=(python)
  fi
fi

gpu_count=0
cuda_available=0
if [[ "$stage_status" == "success" ]]; then
  set +e
  read -r gpu_count cuda_available < <("${WRITER_PY_CMD[@]}" - <<'PY'
try:
    import torch
    print(torch.cuda.device_count(), 1 if torch.cuda.is_available() else 0)
except Exception:
    print(0, 0)
PY
)
  set -e
fi

echo "Observed GPU count: ${gpu_count:-0} (cuda_available=${cuda_available:-0})"

decision_reason=""

if [[ "$stage_status" == "success" ]]; then
  if [[ "${cuda_available:-0}" -eq 0 ]]; then
    stage_status="failure"
    failure_category="hardware"
    exit_code=1
    decision_reason="CUDA is not available; multi-GPU stage requires CUDA."
  elif [[ "${gpu_count:-0}" -lt 2 ]]; then
    stage_status="failure"
    skip_reason="insufficient_hardware"
    failure_category="insufficient_hardware"
    exit_code=1
    decision_reason="Less than 2 GPUs available; multi-GPU stage requires >=2 GPUs."
  else
    # Reviewable evidence for missing multi-GPU support: model is moved to a single CUDA device
    # via .cuda() without any DDP/torch.distributed usage and no distributed launch entrypoint.
    stage_status="skipped"
    skip_reason="repo_not_supported"
    exit_code=0
    decision_reason="Repo does not provide a distributed/multi-GPU entrypoint (no torch.distributed/torchrun/accelerate/deepspeed usage; model uses .cuda() on a single device)."
  fi
fi

git_commit=""
if command -v git >/dev/null 2>&1 && [[ -d "$REPO_ROOT/.git" ]]; then
  git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
fi

prepare_results="$REPO_ROOT/build_output/prepare/results.json"
DATASET_PATH="$REPO_ROOT/benchmark_assets/dataset/prompts.txt"
MODEL_ROOT="$REPO_ROOT/benchmark_assets/model"

"${WRITER_PY_CMD[@]}" - <<PY
import json
from pathlib import Path

def safe_load(path: Path):
  try:
    return json.loads(path.read_text(encoding="utf-8"))
  except Exception:
    return None

assets = {
  "dataset": {"path": "$DATASET_PATH", "source": "", "version": "", "sha256": ""},
  "model": {"path": "$MODEL_ROOT/v1_0", "source": "", "version": "", "sha256": ""},
}

prep = safe_load(Path("$prepare_results"))
if isinstance(prep, dict):
  a = (prep.get("assets") or {})
  if isinstance(a.get("dataset"), dict):
    assets["dataset"].update({k: a["dataset"].get(k,"") for k in ["path","source","version","sha256"]})
  if isinstance(a.get("model"), dict):
    assets["model"].update({k: a["model"].get(k,"") for k in ["path","source","version","sha256"]})

try:
  lines = Path("$LOG_FILE").read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
  excerpt = "\n".join(lines)
except Exception:
  excerpt = ""

payload = {
  "status": "$stage_status",
  "skip_reason": "$skip_reason",
  "exit_code": int("$exit_code"),
  "stage": "multi_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": $timeout_sec,
  "framework": "$framework",
  "assets": assets,
  "meta": {
    "python": "$python_cmd_str",
    "git_commit": "$git_commit",
    "env_vars": {
      "CUDA_VISIBLE_DEVICES": "$devices",
    },
    "decision_reason": "$decision_reason",
    "observed_gpu_count": int("$gpu_count" or 0),
    "observed_cuda_available": bool(int("$cuda_available" or 0)),
  },
  "failure_category": "$failure_category" if "$stage_status" == "failure" else "",
  "error_excerpt": excerpt,
}

Path("$RESULTS_JSON").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

exit "$exit_code"
