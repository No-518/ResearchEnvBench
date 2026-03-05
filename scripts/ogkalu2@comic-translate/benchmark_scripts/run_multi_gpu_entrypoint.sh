#!/usr/bin/env bash
set -u
set -o pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU invocation via the repository's native distributed entrypoint (one step / one item).

Outputs (always, even on failure):
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Options:
  --repo <path>                 Repo root (default: cwd)
  --python <path>               Override python interpreter for metadata and GPU detection
  --report-path <path>          Agent report path (default: /opt/scimlopsbench/report.json)
  --cuda-visible-devices <str>  Default: 0,1 (override with SCIMLOPSBENCH_MULTI_GPU_DEVICES)
  --command "<cmd>"             Full distributed launch command (preferred). If omitted, uses $SCIMLOPSBENCH_MULTI_GPU_COMMAND.

Notes:
  - By default (no --command / env command), this stage is marked SKIPPED because this repo is GUI + ONNXRuntime inference oriented
    and does not provide an official distributed multi-GPU entrypoint.
  - If you provide --command, this script requires >=2 GPUs; if fewer are detected, it exits 1.
  - Provide a launcher consistent with the benchmark contract: torchrun / accelerate launch / deepspeed / lightning.
EOF
}

repo="."
python_bin=""
report_path=""
cuda_visible_devices="${SCIMLOPSBENCH_MULTI_GPU_DEVICES:-0,1}"
command_str="${SCIMLOPSBENCH_MULTI_GPU_COMMAND:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --cuda-visible-devices) cuda_visible_devices="${2:-}"; shift 2 ;;
    --command) command_str="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

# Prevent creating __pycache__ in the repo or environment.
export PYTHONDONTWRITEBYTECODE=1

repo="$(python -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$repo" 2>/dev/null || echo "$repo")"
stage_dir="$repo/build_output/multi_gpu"
mkdir -p "$stage_dir"
log_path="$stage_dir/log.txt"
results_json="$stage_dir/results.json"

: >"$log_path"
exec > >(tee -a "$log_path") 2>&1

echo "stage=multi_gpu"
echo "repo=$repo"
echo "out_dir=$stage_dir"
echo "timestamp_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cd "$repo" || {
  python - <<'PY' >"$results_json"
import json
print(json.dumps({
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "Failed to cd to repo"},
  "failure_category": "entrypoint_not_found",
  "error_excerpt": ""
}, ensure_ascii=False, indent=2))
PY
  exit 1
}

# Load prepared assets (best-effort).
prepare_results="$repo/build_output/prepare/results.json"
dataset_path=""
model_path=""
if [[ -f "$prepare_results" ]]; then
  dataset_path="$(python - "$prepare_results" <<'PY' 2>/dev/null || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print((d.get("assets", {}) or {}).get("dataset", {}).get("path", "") or "")
except Exception:
    print("")
PY
)"
  model_path="$(python - "$prepare_results" <<'PY' 2>/dev/null || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print((d.get("assets", {}) or {}).get("model", {}).get("path", "") or "")
except Exception:
    print("")
PY
)"
fi

# Resolve python interpreter (for metadata + GPU detection).
py_used=""
if [[ -n "$python_bin" ]]; then
  py_used="$python_bin"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  py_used="${SCIMLOPSBENCH_PYTHON}"
else
  rp="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
  if [[ -f "$rp" ]]; then
    py_from_report="$(python - "$rp" <<'PY' 2>/dev/null || true
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
)"
    py_used="$py_from_report"
  fi
fi

if [[ -z "$py_used" ]]; then
  decision_reason="Failed to resolve python interpreter (need --python, SCIMLOPSBENCH_PYTHON, or report.json python_path)."
  STATUS="failure" FAILURE_CATEGORY="missing_report" ERROR_EXCERPT="$decision_reason" \
    PY_USED="" DATASET_PATH="$dataset_path" MODEL_PATH="$model_path" DECISION_REASON="$decision_reason" \
    python - <<'PY' >"$results_json"
import json
import os

assets = {
    "dataset": {"path": os.environ.get("DATASET_PATH", ""), "source": "", "version": "", "sha256": ""},
    "model": {"path": os.environ.get("MODEL_PATH", ""), "source": "", "version": "", "sha256": ""},
}

payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "multi_gpu",
    "task": "infer",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 1200,
    "framework": "unknown",
    "assets": assets,
    "meta": {
        "python": os.environ.get("PY_USED", ""),
        "git_commit": "",
        "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "")},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "missing_report"),
    "error_excerpt": os.environ.get("ERROR_EXCERPT", ""),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
  exit 1
fi

# If no multi-GPU command is provided, mark as skipped (repo does not expose an official distributed entrypoint).
if [[ -z "$command_str" ]]; then
  decision_reason="Skipped: repo provides only GUI usage (README: run comic.py) and ONNXRuntime-based inference; no documented torchrun/accelerate/deepspeed/lightning distributed entrypoint found in repo."
  PREPARE_RESULTS="$prepare_results" DATASET_PATH="$dataset_path" MODEL_PATH="$model_path" PY_USED="$py_used" "$py_used" - <<'PY' >"$results_json"
import json
import os

assets = {
  "dataset": {"path": os.environ.get("DATASET_PATH", ""), "source": "", "version": "", "sha256": ""},
  "model": {"path": os.environ.get("MODEL_PATH", ""), "source": "", "version": "", "sha256": ""}
}
prep = os.environ.get("PREPARE_RESULTS", "")
if prep and os.path.exists(prep):
  try:
    d = json.loads(open(prep, "r", encoding="utf-8").read())
    a = (d.get("assets", {}) or {}) if isinstance(d, dict) else {}
    if isinstance(a.get("dataset"), dict):
      assets["dataset"] = a["dataset"]
    if isinstance(a.get("model"), dict):
      assets["model"] = a["model"]
  except Exception:
    pass

payload = {
  "status": "skipped",
  "skip_reason": "repo_not_supported",
  "exit_code": 0,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": assets,
  "meta": {
    "python": os.environ.get("PY_USED", ""),
    "git_commit": "",
    "env_vars": {"CUDA_VISIBLE_DEVICES": ""},
    "decision_reason": "Skipped: no official distributed multi-GPU entrypoint for this repo (GUI + ONNXRuntime inference)."
  },
  "failure_category": "not_applicable",
  "error_excerpt": ""
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
  echo "SKIPPED: repo_not_supported (no official distributed multi-GPU entrypoint)"
  exit 0
fi

# Detect GPU count (prefer nvidia-smi; fallback to torch if available).
gpu_count="$(
  "$py_used" - <<'PY' 2>/dev/null || true
import os, shutil, subprocess, sys

def count_nvidia_smi() -> int:
    if not shutil.which("nvidia-smi"):
        return 0
    try:
        out = subprocess.check_output(["nvidia-smi", "-L"], text=True, stderr=subprocess.DEVNULL)
        lines = [ln for ln in out.splitlines() if ln.strip().startswith("GPU")]
        return len(lines)
    except Exception:
        return 0

def count_torch() -> int:
    try:
        import torch
        return int(torch.cuda.device_count())
    except Exception:
        return 0

print(count_nvidia_smi() or count_torch())
PY
)"

gpu_count="${gpu_count:-0}"
echo "detected_gpu_count=$gpu_count"

if [[ "$gpu_count" -lt 2 ]]; then
  decision_reason="Multi-GPU stage requires >=2 GPUs; detected $gpu_count."
  STATUS="failure" FAILURE_CATEGORY="runtime" ERROR_EXCERPT="Need >=2 GPUs; detected $gpu_count" \
    PY_USED="$py_used" DATASET_PATH="$dataset_path" MODEL_PATH="$model_path" DECISION_REASON="$decision_reason" \
    CUDA_VISIBLE_DEVICES="$cuda_visible_devices" COMMAND_STR="$command_str" OBS_GPU_COUNT="$gpu_count" \
    PREPARE_RESULTS="$prepare_results" "$py_used" - <<'PY' >"$results_json"
import json
import os

assets = {
    "dataset": {"path": os.environ.get("DATASET_PATH", ""), "source": "", "version": "", "sha256": ""},
    "model": {"path": os.environ.get("MODEL_PATH", ""), "source": "", "version": "", "sha256": ""},
}
prep = os.environ.get("PREPARE_RESULTS", "")
if prep and os.path.exists(prep):
    try:
        d = json.loads(open(prep, "r", encoding="utf-8").read())
        a = (d.get("assets", {}) or {}) if isinstance(d, dict) else {}
        if isinstance(a.get("dataset"), dict):
            assets["dataset"] = a["dataset"]
        if isinstance(a.get("model"), dict):
            assets["model"] = a["model"]
    except Exception:
        pass

payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "multi_gpu",
    "task": "infer",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 1200,
    "framework": "unknown",
    "assets": assets,
    "meta": {
        "python": os.environ.get("PY_USED", ""),
        "git_commit": "",
        "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "")},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "runtime"),
    "error_excerpt": os.environ.get("ERROR_EXCERPT", ""),
    "observed": {"gpu_count": int(os.environ.get("OBS_GPU_COUNT", "0") or 0)},
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
  exit 1
fi

# Constrain caches (avoid writing outside benchmark_assets/cache).
cache_root="$repo/benchmark_assets/cache"
export HF_HOME="$cache_root/hf_home"
export HUGGINGFACE_HUB_CACHE="$cache_root/huggingface_hub"
export HF_HUB_CACHE="$cache_root/huggingface_hub"
export TRANSFORMERS_CACHE="$cache_root/transformers"
export HF_DATASETS_CACHE="$cache_root/datasets"
export TORCH_HOME="$cache_root/torch"
export XDG_CACHE_HOME="$cache_root/xdg_cache"
export XDG_CONFIG_HOME="$cache_root/xdg_config"
export XDG_DATA_HOME="$cache_root/xdg_data"

export CUDA_VISIBLE_DEVICES="$cuda_visible_devices"
export HF_HUB_OFFLINE="1"

case "$command_str" in
  torchrun*|deepspeed*|lightning*|accelerate\ launch*)
    ;;
  *)
    echo "Provided --command does not appear to use an allowed distributed launcher (torchrun/accelerate launch/deepspeed/lightning)." >&2
    decision_reason="Command rejected: expected distributed launcher torchrun/accelerate launch/deepspeed/lightning."
    STATUS="failure" FAILURE_CATEGORY="args_unknown" ERROR_EXCERPT="disallowed launcher; expected torchrun/accelerate launch/deepspeed/lightning" \
      PY_USED="$py_used" DATASET_PATH="$dataset_path" MODEL_PATH="$model_path" DECISION_REASON="$decision_reason" \
      CUDA_VISIBLE_DEVICES="$cuda_visible_devices" COMMAND_STR="$command_str" OBS_GPU_COUNT="$gpu_count" \
      "$py_used" - <<'PY' >"$results_json"
import json
import os

assets = {
    "dataset": {"path": os.environ.get("DATASET_PATH", ""), "source": "", "version": "", "sha256": ""},
    "model": {"path": os.environ.get("MODEL_PATH", ""), "source": "", "version": "", "sha256": ""},
}

payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "multi_gpu",
    "task": "infer",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 1200,
    "framework": "unknown",
    "assets": assets,
    "meta": {
        "python": os.environ.get("PY_USED", ""),
        "git_commit": "",
        "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "")},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "args_unknown"),
    "error_excerpt": os.environ.get("ERROR_EXCERPT", ""),
    "observed": {"gpu_count": int(os.environ.get("OBS_GPU_COUNT", "0") or 0)},
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
    exit 1
    ;;
esac

decision_reason="Multi-GPU stage requires an official distributed repo entrypoint. Provide --command or set SCIMLOPSBENCH_MULTI_GPU_COMMAND. Ensure batch_size=1 and steps=1."

echo "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
echo "command=$command_str"

timeout 1200s bash -lc "$command_str"
rc=$?

status="failure"
failure_category="runtime"
if [[ "$rc" -eq 0 ]]; then
  status="success"
  failure_category="unknown"
fi
error_excerpt="$(tail -n 220 "$log_path" 2>/dev/null || true)"

STATUS="$status" FAILURE_CATEGORY="$failure_category" ERROR_EXCERPT="$error_excerpt" \
  PY_USED="$py_used" DATASET_PATH="$dataset_path" MODEL_PATH="$model_path" DECISION_REASON="$decision_reason" \
  COMMAND_STR="$command_str" CUDA_VISIBLE_DEVICES="$cuda_visible_devices" \
  PREPARE_RESULTS="$prepare_results" "$py_used" - <<'PY' >"$results_json"
import json
import os

status = os.environ.get("STATUS", "failure")
assets = {
    "dataset": {"path": os.environ.get("DATASET_PATH", ""), "source": "", "version": "", "sha256": ""},
    "model": {"path": os.environ.get("MODEL_PATH", ""), "source": "", "version": "", "sha256": ""},
}
prep = os.environ.get("PREPARE_RESULTS", "")
if prep and os.path.exists(prep):
    try:
        d = json.loads(open(prep, "r", encoding="utf-8").read())
        a = (d.get("assets", {}) or {}) if isinstance(d, dict) else {}
        if isinstance(a.get("dataset"), dict):
            assets["dataset"] = a["dataset"]
        if isinstance(a.get("model"), dict):
            assets["model"] = a["model"]
    except Exception:
        pass

payload = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": 0 if status == "success" else 1,
    "stage": "multi_gpu",
    "task": "infer",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 1200,
    "framework": "unknown",
    "assets": assets,
    "meta": {
        "python": os.environ.get("PY_USED", ""),
        "git_commit": "",
        "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "")},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": os.environ.get("ERROR_EXCERPT", ""),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

if [[ "$status" == "success" ]]; then
  exit 0
fi
exit 1
