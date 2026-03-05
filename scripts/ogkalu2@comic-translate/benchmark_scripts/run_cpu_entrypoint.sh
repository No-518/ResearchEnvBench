#!/usr/bin/env bash
set -u
set -o pipefail

usage() {
  cat <<'EOF'
Run a minimal CPU invocation via the repository's native entrypoint (one step / one item).

Default behavior (no --command):
  Runs a minimal, repo-native inference step using `modules/detection/rtdetr_v2_onnx.py`
  (RT-DETR-v2 ONNX text/bubble detector) on the prepared dataset image.

Outputs (always, even on failure):
  build_output/cpu/log.txt
  build_output/cpu/results.json

Options:
  --repo <path>            Repo root (default: cwd)
  --python <path>          Override python interpreter
  --report-path <path>     Agent report path (default: /opt/scimlopsbench/report.json)
  --command "<cmd>"        Full command to run. If omitted, uses $SCIMLOPSBENCH_CPU_COMMAND, otherwise uses the default detector inference.
EOF
}

repo="."
python_bin=""
report_path=""
command_str="${SCIMLOPSBENCH_CPU_COMMAND:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --command) command_str="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

# Prevent creating __pycache__ in the repo or environment.
export PYTHONDONTWRITEBYTECODE=1

repo="$(python -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$repo" 2>/dev/null || echo "$repo")"
stage_dir="$repo/build_output/cpu"
mkdir -p "$stage_dir"
log_path="$stage_dir/log.txt"
results_json="$stage_dir/results.json"

: >"$log_path"
exec > >(tee -a "$log_path") 2>&1

echo "stage=cpu"
echo "repo=$repo"
echo "out_dir=$stage_dir"
echo "timestamp_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cd "$repo" || {
  cat >"$results_json" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "cpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 600,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "Failed to cd to repo"},
  "failure_category": "entrypoint_not_found",
  "error_excerpt": ""
}
JSON
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

# Resolve python interpreter (must comply with report.json unless overridden).
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

# Hard requirement: if python cannot be resolved, fail with missing_report.
if [[ -z "$py_used" ]]; then
  decision_reason="Failed to resolve python interpreter (need --python, SCIMLOPSBENCH_PYTHON, or report.json python_path)."
  STATUS="failure" FAILURE_CATEGORY="missing_report" ERROR_EXCERPT="$decision_reason" \
    PY_USED="" DATASET_PATH="$dataset_path" MODEL_PATH="$model_path" DECISION_REASON="$decision_reason" \
    python - <<'PY' >"$results_json"
import json
import os

payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "cpu",
    "task": "infer",
    "command": "",
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": os.environ.get("DATASET_PATH", ""), "source": "", "version": "", "sha256": ""},
        "model": {"path": os.environ.get("MODEL_PATH", ""), "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PY_USED", ""),
        "git_commit": "",
        "env_vars": {"CUDA_VISIBLE_DEVICES": ""},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "missing_report"),
    "error_excerpt": os.environ.get("ERROR_EXCERPT", ""),
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

# Force CPU.
export CUDA_VISIBLE_DEVICES=""
export HF_HUB_OFFLINE="1"

decision_reason="Default CPU inference: run RTDetrV2ONNXDetection on a single prepared image (1 item = 1 step). Patches hf_hub_download to use benchmark_assets/model local files (offline) and disables optional font-model downloads to avoid writing outside benchmark_assets/build_output; forces CPU via CUDA_VISIBLE_DEVICES=''."

# If no command provided, run default repo-native inference step.
run_mode="custom_command"
if [[ -z "$command_str" ]]; then
  run_mode="inline_python"
  if [[ -z "$dataset_path" ]]; then
    STATUS="failure" FAILURE_CATEGORY="data" ERROR_EXCERPT="prepare stage missing dataset path (run prepare_assets.sh first)" \
      PY_USED="$py_used" DATASET_PATH="$dataset_path" MODEL_PATH="$model_path" DECISION_REASON="$decision_reason" \
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
    "stage": "cpu",
    "task": "infer",
    "command": "",
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": assets,
    "meta": {
        "python": os.environ.get("PY_USED", ""),
        "git_commit": "",
        "env_vars": {"CUDA_VISIBLE_DEVICES": ""},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "data"),
    "error_excerpt": os.environ.get("ERROR_EXCERPT", ""),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
    exit 1
  fi
  if [[ -z "$model_path" ]]; then
    STATUS="failure" FAILURE_CATEGORY="model" ERROR_EXCERPT="prepare stage missing model path (run prepare_assets.sh first)" \
      PY_USED="$py_used" DATASET_PATH="$dataset_path" MODEL_PATH="$model_path" DECISION_REASON="$decision_reason" \
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
    "stage": "cpu",
    "task": "infer",
    "command": "",
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": assets,
    "meta": {
        "python": os.environ.get("PY_USED", ""),
        "git_commit": "",
        "env_vars": {"CUDA_VISIBLE_DEVICES": ""},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "model"),
    "error_excerpt": os.environ.get("ERROR_EXCERPT", ""),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
    exit 1
  fi

  command_str="$py_used - <inline_python> \"$dataset_path\" \"$model_path\""
fi

echo "command=$command_str"

rc=0
if [[ "$run_mode" == "inline_python" ]]; then
  timeout 600s "$py_used" - "$dataset_path" "$model_path" <<'PY'
import sys
from pathlib import Path

import numpy as np
from PIL import Image

import modules.detection.base as det_base
import modules.detection.rtdetr_v2_onnx as rtdetr_onnx

dataset_path = Path(sys.argv[1]).expanduser().resolve()
model_dir = Path(sys.argv[2]).expanduser().resolve()

if not dataset_path.exists():
    raise FileNotFoundError(f"dataset path not found: {dataset_path}")

if not model_dir.exists():
    raise FileNotFoundError(f"model dir not found: {model_dir}")

expected = model_dir / "detector.onnx"
cfg = model_dir / "config.json"
if not expected.exists():
    raise FileNotFoundError(f"expected model artifact missing: {expected}")
if not cfg.exists():
    raise FileNotFoundError(f"expected model artifact missing: {cfg}")

# Prevent the detection pipeline from downloading optional font models (and writing outside benchmark dirs).
class _DummyFontEngine:
    def process(self, image: np.ndarray) -> dict:
        return {"available": False}

def _dummy_create_engine(cls, settings, backend: str = "onnx"):
    return _DummyFontEngine()

det_base.FontEngineFactory.create_engine = classmethod(_dummy_create_engine)  # type: ignore[attr-defined]

# Repo code downloads via huggingface_hub by default; patch it to use our prepared local files.
def _local_hf_hub_download(repo_id: str, filename: str, *args, **kwargs) -> str:
    p = model_dir / filename
    if p.exists():
        return str(p)
    raise FileNotFoundError(f"hf_hub_download requested missing local file: {p} (repo_id={repo_id}, filename={filename})")

rtdetr_onnx.hf_hub_download = _local_hf_hub_download

img = Image.open(dataset_path).convert("RGB")
arr = np.asarray(img)

det = rtdetr_onnx.RTDetrV2ONNXDetection(settings=None)
det.model_dir = str(model_dir)
det.initialize(device="cpu", confidence_threshold=0.3)
blks = det.detect(arr)
print(f"cpu_infer_ok num_blocks={len(blks)} image={dataset_path.name}")
PY
  rc=$?
else
  timeout 600s bash -lc "$command_str"
  rc=$?
fi

status="failure"
failure_category="runtime"
if [[ "$rc" -eq 0 ]]; then
  status="success"
  failure_category="unknown"
fi

STATUS="$status" FAILURE_CATEGORY="$failure_category" \
  PY_USED="$py_used" DATASET_PATH="$dataset_path" MODEL_PATH="$model_path" DECISION_REASON="$decision_reason" \
  COMMAND_STR="$command_str" LOG_PATH="$log_path" PREPARE_RESULTS="$prepare_results" \
  "$py_used" - <<'PY' >"$results_json"
import json
import os
from pathlib import Path

def tail(path: Path, max_lines: int = 240) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        return "".join(lines[-max_lines:])
    except Exception:
        return ""

status = os.environ.get("STATUS", "failure")
log_path = Path(os.environ.get("LOG_PATH", ""))
prep_path = Path(os.environ.get("PREPARE_RESULTS", ""))

assets = {
    "dataset": {"path": os.environ.get("DATASET_PATH", ""), "source": "", "version": "", "sha256": ""},
    "model": {"path": os.environ.get("MODEL_PATH", ""), "source": "", "version": "", "sha256": ""},
}
if prep_path and prep_path.exists():
    try:
        d = json.loads(prep_path.read_text(encoding="utf-8"))
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
    "stage": "cpu",
    "task": "infer",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 600,
    "framework": "unknown",
    "assets": assets,
    "meta": {
        "python": os.environ.get("PY_USED", ""),
        "git_commit": "",
        "env_vars": {"CUDA_VISIBLE_DEVICES": ""},
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": "" if status == "success" else tail(log_path, 240),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

if [[ "$status" == "success" ]]; then
  exit 0
fi
exit 1
