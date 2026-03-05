#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Download/prepare benchmark assets (dataset + minimal model weights).

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Optional:
  --python <path>        Override python interpreter (otherwise resolved from report.json)
  --report-path <path>   Override report path (default: /opt/scimlopsbench/report.json)
EOF
}

python_bin=""
report_path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
stage_dir="$repo_root/build_output/prepare"
mkdir -p "$stage_dir"
log_file="$stage_dir/log.txt"
results_json="$stage_dir/results.json"

resolver=(python3 "$script_dir/runner.py" resolve-python)
if [[ -n "$python_bin" ]]; then
  resolver+=(--python "$python_bin")
fi
if [[ -n "$report_path" ]]; then
  resolver+=(--report-path "$report_path")
fi

py_bin="$("${resolver[@]}")" || true
if [[ -z "${py_bin:-}" ]]; then
  cat >"$results_json" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "python3 benchmark_scripts/runner.py resolve-python",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": null,
    "git_commit": null,
    "env_vars": {},
    "decision_reason": "Unable to resolve python from report for prepare stage."
  },
  "failure_category": "missing_report",
  "error_excerpt": "failed to resolve python from report"
}
EOF
  echo "failed to resolve python from report" >"$log_file"
  exit 1
fi

export LIGHTLY_TRAIN_EVENTS_DISABLED=1
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HOME="$repo_root/benchmark_assets/cache/hf"
export TRANSFORMERS_CACHE="$repo_root/benchmark_assets/cache/hf/transformers"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export LIGHTLY_TRAIN_CACHE_DIR="$repo_root/benchmark_assets/cache/lightly-train"

mkdir -p "$repo_root/benchmark_assets/cache" "$repo_root/benchmark_assets/dataset" "$repo_root/benchmark_assets/model"

exec >"$log_file" 2>&1

echo "[prepare] python=$py_bin"
echo "[prepare] repo_root=$repo_root"

PYTHON_BIN="$py_bin" REPORT_PATH="$report_path" REPO_ROOT="$repo_root" RESULTS_JSON="$results_json" \
"$py_bin" - <<'PY'
import json
import os
import pathlib
import shutil
import subprocess
import sys
import traceback
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

REPO_ROOT = pathlib.Path(os.environ["REPO_ROOT"]).resolve()
RESULTS_JSON = pathlib.Path(os.environ["RESULTS_JSON"])

STAGE = "prepare"
TIMEOUT_SEC = 1200

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def git_commit() -> str | None:
    try:
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None
    except Exception:
        return None

def file_sha256(path: pathlib.Path) -> str:
    h = sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def download(url: str, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    if tmp.exists():
        tmp.unlink()
    with urllib.request.urlopen(url, timeout=60) as r, tmp.open("wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dest)

@dataclass(frozen=True)
class AssetSpec:
    kind: str
    name: str
    url: str
    version: str
    cache_path: pathlib.Path
    final_path: pathlib.Path

cache_dir = REPO_ROOT / "benchmark_assets" / "cache"
dataset_dir = REPO_ROOT / "benchmark_assets" / "dataset"
model_dir = REPO_ROOT / "benchmark_assets" / "model"

dataset_zip = cache_dir / "coco128_unlabeled.zip"
dataset_final = dataset_dir / "coco128_unlabeled"
dataset = AssetSpec(
    kind="dataset",
    name="coco128_unlabeled",
    url="https://github.com/lightly-ai/coco128_unlabeled/releases/download/v0.0.1/coco128_unlabeled.zip",
    version="v0.0.1",
    cache_path=dataset_zip,
    final_path=dataset_final,
)

model_weights = cache_dir / "dinov3_vitt16_distillationv2.pth"
model_final_dir = model_dir / "dinov3_vitt16"
model_final_file = model_final_dir / "dinov3_vitt16_distillationv2.pth"
model = AssetSpec(
    kind="model",
    name="dinov3/vitt16",
    url="https://lightly-train-checkpoints.s3.us-east-1.amazonaws.com/dinov3/dinov3_vitt16_distillationv2.pth",
    version="distillationv2",
    cache_path=model_weights,
    final_path=model_final_dir,
)

status = "success"
failure_category = ""
error_excerpt = ""

assets_out: dict[str, dict[str, str]] = {
    "dataset": {"path": str(dataset_final), "source": dataset.url, "version": dataset.version, "sha256": ""},
    "model": {"path": str(model_final_dir), "source": model.url, "version": model.version, "sha256": ""},
}

def fail(cat: str, msg: str) -> None:
    global status, failure_category, error_excerpt
    status = "failure"
    failure_category = cat
    error_excerpt = msg

def ensure_dataset() -> None:
    # Download zip if needed.
    if not dataset.cache_path.exists():
        try:
            print(f"[prepare] downloading dataset: {dataset.url} -> {dataset.cache_path}")
            download(dataset.url, dataset.cache_path)
        except Exception as exc:
            if dataset.cache_path.exists():
                print(f"[prepare] dataset download failed, using cached file: {exc}")
            else:
                raise
    assets_out["dataset"]["sha256"] = file_sha256(dataset.cache_path)

    # Extract if needed.
    if dataset.final_path.exists():
        print(f"[prepare] dataset already present: {dataset.final_path}")
        return
    print(f"[prepare] extracting dataset zip -> {dataset_dir}")
    dataset_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dataset.cache_path, "r") as z:
        z.extractall(dataset_dir)
    if not dataset.final_path.exists():
        raise RuntimeError(
            f"dataset extraction succeeded but expected dataset dir missing: {dataset.final_path} (extract_root={dataset_dir})"
        )

def ensure_model() -> None:
    # Download weights if needed.
    if not model.cache_path.exists():
        try:
            print(f"[prepare] downloading model weights: {model.url} -> {model.cache_path}")
            download(model.url, model.cache_path)
        except Exception as exc:
            if model.cache_path.exists():
                print(f"[prepare] model download failed, using cached file: {exc}")
            else:
                raise
    weights_sha = file_sha256(model.cache_path)

    # Copy into final model dir.
    model_final_dir.mkdir(parents=True, exist_ok=True)
    if not model_final_file.exists():
        print(f"[prepare] copying model weights -> {model_final_file}")
        shutil.copy2(model.cache_path, model_final_file)
    assets_out["model"]["sha256"] = file_sha256(model_final_file)

    # Verify resolution.
    if not model_final_file.exists():
        raise RuntimeError(
            f"model download reported success but resolved model file missing: {model_final_file} (search root={model_final_dir})"
        )
    if assets_out["model"]["sha256"] != weights_sha:
        print("[prepare] WARNING: sha256 mismatch between cache and final model file")

try:
    ensure_dataset()
    ensure_model()
except Exception:
    tb = traceback.format_exc()
    # Distinguish download vs model resolution.
    if "urlopen" in tb or "download" in tb:
        fail("download_failed", tb)
    else:
        # If download succeeded but file cannot be resolved/verified, classify as model.
        if model.cache_path.exists() and not model_final_file.exists():
            fail("model", tb)
        elif dataset.cache_path.exists() and not dataset.final_path.exists():
            fail("data", tb)
        else:
            fail("unknown", tb)

exit_code = 0 if status in ("success", "skipped") else 1

results = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": STAGE,
    "task": "download",
    "command": "benchmark_scripts/prepare_assets.sh",
    "timeout_sec": TIMEOUT_SEC,
    "framework": "unknown",
    "assets": assets_out,
    "meta": {
        "python": os.environ.get("PYTHON_BIN", sys.executable),
        "git_commit": git_commit(),
        "env_vars": {
            "LIGHTLY_TRAIN_CACHE_DIR": os.environ.get("LIGHTLY_TRAIN_CACHE_DIR", ""),
            "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", ""),
            "HF_HOME": os.environ.get("HF_HOME", ""),
            "TORCH_HOME": os.environ.get("TORCH_HOME", ""),
            "LIGHTLY_TRAIN_EVENTS_DISABLED": os.environ.get("LIGHTLY_TRAIN_EVENTS_DISABLED", ""),
        },
        "decision_reason": (
            "Dataset and model chosen from examples/notebooks/distillation.ipynb: "
            "coco128_unlabeled + dinov3/vitt16 weights; anonymous downloads; small for benchmark."
        ),
        "started_utc": utc_now(),
        "finished_utc": utc_now(),
    },
    "failure_category": failure_category,
    "error_excerpt": error_excerpt[-4000:] if error_excerpt else "",
}
RESULTS_JSON.parent.mkdir(parents=True, exist_ok=True)
RESULTS_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
raise SystemExit(exit_code)
PY
