#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model download) into:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Options:
  --python <path>        Python executable to use (default: from /opt/scimlopsbench/report.json)
  --report-path <path>   Override report path (default: /opt/scimlopsbench/report.json)
  --model-id <id>        Hugging Face repo id (default: metavoiceio/metavoice-1B-v0.1)
  --timeout-sec <n>      Default: 1200 (recorded only; download itself is not forcibly killed)
EOF
}

python_bin=""
report_path=""
model_id="metavoiceio/metavoice-1B-v0.1"
timeout_sec="1200"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --model-id) model_id="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
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

stage_dir="$repo_root/build_output/prepare"
mkdir -p "$stage_dir"
log_path="$stage_dir/log.txt"
results_json="$stage_dir/results.json"
touch "$log_path" "$results_json"

exec > >(tee -a "$log_path") 2>&1

echo "[prepare] repo_root=$repo_root"
echo "[prepare] model_id=$model_id"

if [[ -z "$python_bin" ]]; then
  python_bin="$(
    REPORT_PATH="$report_path" python3 - <<'PY' 2>/dev/null || true
import json, os, pathlib
rp = os.environ.get("REPORT_PATH") or os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"
p = pathlib.Path(rp)
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print(data.get("python_path",""))
except Exception:
    print("")
PY
  )"
fi

if [[ -z "$python_bin" ]]; then
  echo "[prepare] ERROR: could not resolve python_path from report.json and --python not provided" >&2
  REPO_ROOT="$repo_root" STAGE_DIR="$stage_dir" TIMEOUT_SEC="$timeout_sec" python3 - <<'PY' || true
import json, os, pathlib, subprocess, time
repo_root = pathlib.Path(os.environ.get("REPO_ROOT",".")).resolve()
stage_dir = pathlib.Path(os.environ["STAGE_DIR"]).resolve()
log_path = stage_dir / "log.txt"
results_path = stage_dir / "results.json"
def git_commit():
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=repo_root, text=True, timeout=5).strip()
    except Exception:
        return ""
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": int(os.environ.get("TIMEOUT_SEC","1200")),
  "framework": "unknown",
  "assets": {"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta": {
    "python": "",
    "git_commit": git_commit(),
    "env_vars": {},
    "decision_reason": "Missing/invalid /opt/scimlopsbench/report.json and no --python provided; cannot resolve python_path."
  },
  "failure_category": "missing_report",
  "error_excerpt": "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:]),
}
results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
fi

echo "[prepare] using python=$python_bin"

# Force all caches into repository-local benchmark_assets/cache/
export HOME="$repo_root/benchmark_assets/cache/home"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HOME="$repo_root/benchmark_assets/cache/huggingface"
export HF_HUB_DISABLE_TELEMETRY="1"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export TMPDIR="$repo_root/build_output/prepare/tmp"
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$HF_HOME" "$TORCH_HOME" "$TMPDIR"

REPO_ROOT="$repo_root" STAGE_DIR="$stage_dir" TIMEOUT_SEC="$timeout_sec" MODEL_ID="$model_id" REPORT_PATH="$report_path" \
  "$python_bin" - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

repo_root = Path(os.environ["REPO_ROOT"]).resolve()
stage_dir = Path(os.environ["STAGE_DIR"]).resolve()
timeout_sec = int(os.environ.get("TIMEOUT_SEC", "1200"))
model_id = os.environ.get("MODEL_ID", "metavoiceio/metavoice-1B-v0.1")

dataset_dir = repo_root / "benchmark_assets" / "dataset"
cache_dir = repo_root / "benchmark_assets" / "cache"
model_link_root = repo_root / "benchmark_assets" / "model"

dataset_dir.mkdir(parents=True, exist_ok=True)
cache_dir.mkdir(parents=True, exist_ok=True)
model_link_root.mkdir(parents=True, exist_ok=True)

log_path = stage_dir / "log.txt"
results_path = stage_dir / "results.json"

def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, timeout=5).strip()
    except Exception:
        return ""

def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def safe_write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def tail_log(max_lines: int = 220) -> str:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""

def record_failure(category: str, decision_reason: str, assets: Dict[str, Any]) -> int:
    payload = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "prepare",
        "task": "download",
        "command": f"benchmark_scripts/prepare_assets.sh --model-id {model_id}",
        "timeout_sec": timeout_sec,
        "framework": "unknown",
        "assets": assets,
        "meta": {
            "python": sys.version.split()[0],
            "git_commit": git_commit(),
            "env_vars": {
                k: os.environ.get(k, "")
                for k in [
                    "HOME",
                    "XDG_CACHE_HOME",
                    "HF_HOME",
                    "HF_HUB_DISABLE_TELEMETRY",
                    "TORCH_HOME",
                    "TMPDIR",
                    "HF_TOKEN",
                ]
            },
            "decision_reason": decision_reason,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "failure_category": category,
        "error_excerpt": tail_log(),
    }
    safe_write_json(results_path, payload)
    return 1

assets_unknown = {
    "dataset": {"path": str(dataset_dir), "source": "repo", "version": git_commit(), "sha256": ""},
    "model": {"path": "", "source": model_id, "version": "", "sha256": ""},
}

try:
    # Dataset preparation: copy in-repo sample assets into benchmark_assets/dataset
    dataset_sources = [
        (repo_root / "assets" / "bria.mp3", dataset_dir / "bria.mp3"),
        (repo_root / "data" / "caption.txt", dataset_dir / "caption.txt"),
        (repo_root / "data" / "audio.wav", dataset_dir / "audio.wav"),
        (repo_root / "datasets" / "sample_dataset.csv", dataset_dir / "sample_dataset.csv"),
        (repo_root / "datasets" / "sample_val_dataset.csv", dataset_dir / "sample_val_dataset.csv"),
    ]
    for src, dst in dataset_sources:
        if not src.exists():
            raise FileNotFoundError(f"Missing dataset source file: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    dataset_manifest = {
        "source": "repo",
        "version": git_commit(),
        "files": [],
    }
    for _, dst in dataset_sources:
        dataset_manifest["files"].append(
            {
                "path": str(dst.relative_to(repo_root)),
                "size_bytes": dst.stat().st_size,
                "sha256": sha256_file(dst),
            }
        )
    dataset_manifest_path = dataset_dir / "manifest.json"
    safe_write_json(dataset_manifest_path, dataset_manifest)
    dataset_sha = sha256_file(dataset_manifest_path)

    assets_unknown["dataset"]["sha256"] = dataset_sha

except Exception as exc:
    return_code = record_failure("data", f"Dataset preparation failed: {exc}", assets_unknown)
    raise SystemExit(return_code)

# Model download: Hugging Face snapshot_download cached under benchmark_assets/cache via HF_HOME
expected_model_files = ["first_stage.pt", "second_stage.pt", "speaker_encoder.pt"]

model_manifest_path = model_link_root / "manifest.json"
model_link_name = re.sub(r"[^A-Za-z0-9._-]+", "_", model_id)
model_link_path = model_link_root / model_link_name

def model_ready(path: Path) -> bool:
    return path.exists() and all((path / f).is_file() for f in expected_model_files)

def extract_version_from_path(p: str) -> str:
    m = re.search(r"([0-9a-f]{40})", p)
    return m.group(1) if m else ""

snapshot_path: Optional[str] = None
download_used_local_only = False
download_error: Optional[str] = None

try:
    from huggingface_hub import snapshot_download  # type: ignore
except Exception as exc:
    raise SystemExit(record_failure("deps", f"Missing dependency huggingface_hub: {exc}", assets_unknown))

if model_ready(model_link_path):
    snapshot_path = str(model_link_path.resolve())
    download_used_local_only = True
else:
    try:
        snapshot_path = snapshot_download(
            repo_id=model_id,
            allow_patterns=expected_model_files,
        )
        print(f"[prepare] snapshot_download returned: {snapshot_path}")
    except Exception as exc:
        download_error = str(exc)
        print(f"[prepare] snapshot_download failed: {exc}")
        try:
            snapshot_path = snapshot_download(
                repo_id=model_id,
                allow_patterns=expected_model_files,
                local_files_only=True,
            )
            download_used_local_only = True
            print(f"[prepare] local_files_only snapshot returned: {snapshot_path}")
        except Exception as exc2:
            download_error = (download_error or "") + f"\n(local_files_only) {exc2}"
            snapshot_path = None

if not snapshot_path:
    raise SystemExit(
        record_failure(
            "download_failed",
            "Model download failed and no cached snapshot is available. "
            "If the model is gated/private, set HF_TOKEN. "
            f"Error: {download_error}",
            assets_unknown,
        )
    )

snapshot_dir = Path(snapshot_path)
if not model_ready(snapshot_dir):
    # Try to locate under benchmark_assets/cache as last resort (do not assume hub cache layout; only search under our cache root)
    candidates: List[Path] = []
    for f in expected_model_files:
        for p in cache_dir.rglob(f):
            candidates.append(p.parent)
    candidates = sorted({c for c in candidates})
    resolved = None
    for c in candidates:
        if model_ready(c):
            resolved = c
            break
    if resolved is None:
        raise SystemExit(
            record_failure(
                "model",
                "Download appeared to succeed, but expected model artifacts were not found/verified. "
                f"snapshot_download returned: {snapshot_path}. Searched under: {cache_dir}",
                assets_unknown,
            )
        )
    snapshot_dir = resolved
    snapshot_path = str(snapshot_dir)

# Link/copy model snapshot into benchmark_assets/model/
try:
    if model_link_path.exists() or model_link_path.is_symlink():
        if model_link_path.is_symlink() or model_link_path.is_file():
            model_link_path.unlink()
        elif model_link_path.is_dir():
            # Keep if it's already the right target; otherwise remove.
            if not model_ready(model_link_path):
                shutil.rmtree(model_link_path)
    model_link_root.mkdir(parents=True, exist_ok=True)
    try:
        model_link_path.symlink_to(snapshot_dir, target_is_directory=True)
    except Exception:
        # Symlinks may be unavailable; fall back to copying expected files only.
        model_link_path.mkdir(parents=True, exist_ok=True)
        for f in expected_model_files:
            shutil.copy2(snapshot_dir / f, model_link_path / f)
except Exception as exc:
    raise SystemExit(record_failure("model", f"Failed to link/copy model into benchmark_assets/model: {exc}", assets_unknown))

resolved_model_dir = model_link_path.resolve() if model_link_path.is_symlink() else model_link_path
if not model_ready(resolved_model_dir):
    raise SystemExit(
        record_failure(
            "model",
            f"Resolved model directory missing expected files: {resolved_model_dir}",
            assets_unknown,
        )
    )

# Compute/store model manifest (skip re-hashing if already present with matching sizes)
existing_manifest = None
try:
    if model_manifest_path.exists():
        existing_manifest = json.loads(model_manifest_path.read_text(encoding="utf-8"))
except Exception:
    existing_manifest = None

model_files_info = []
reuse_hashes = False
if isinstance(existing_manifest, dict):
    existing_files = existing_manifest.get("files")
    if isinstance(existing_files, list):
        by_name = {f.get("name"): f for f in existing_files if isinstance(f, dict)}
        reuse_hashes = True
        for name in expected_model_files:
            p = resolved_model_dir / name
            if not p.exists():
                reuse_hashes = False
                break
            rec = by_name.get(name) or {}
            if int(rec.get("size_bytes", -1)) != p.stat().st_size or not rec.get("sha256"):
                reuse_hashes = False
                break

for name in expected_model_files:
    p = resolved_model_dir / name
    if reuse_hashes and isinstance(existing_manifest, dict):
        rec = next((f for f in existing_manifest.get("files", []) if isinstance(f, dict) and f.get("name") == name), None)
        if isinstance(rec, dict):
            model_files_info.append({"name": name, "size_bytes": int(rec["size_bytes"]), "sha256": str(rec["sha256"])})
            continue
    model_files_info.append({"name": name, "size_bytes": p.stat().st_size, "sha256": sha256_file(p)})

model_version = extract_version_from_path(str(snapshot_dir))
model_manifest = {
    "source": model_id,
    "version": model_version,
    "snapshot_dir": str(snapshot_dir),
    "resolved_model_dir": str(resolved_model_dir),
    "files": model_files_info,
    "download_used_local_only": download_used_local_only,
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
safe_write_json(model_manifest_path, model_manifest)
model_sha = sha256_file(model_manifest_path)

assets = {
    "dataset": {"path": str(dataset_dir), "source": "repo", "version": git_commit(), "sha256": dataset_sha},
    "model": {"path": str(model_link_path), "source": model_id, "version": model_version, "sha256": model_sha},
}

payload = {
    "status": "success",
    "skip_reason": "not_applicable",
    "exit_code": 0,
    "stage": "prepare",
    "task": "download",
    "command": f"benchmark_scripts/prepare_assets.sh --model-id {model_id}",
    "timeout_sec": timeout_sec,
    "framework": "unknown",
    "assets": assets,
    "meta": {
        "python": sys.version.split()[0],
        "git_commit": git_commit(),
        "env_vars": {
            k: os.environ.get(k, "")
            for k in [
                "HOME",
                "XDG_CACHE_HOME",
                "HF_HOME",
                "HF_HUB_DISABLE_TELEMETRY",
                "TORCH_HOME",
                "TMPDIR",
            ]
        },
        "decision_reason": (
            "Dataset uses in-repo sample files (assets/bria.mp3, data/*, datasets/*.csv). "
            "Model is downloaded via huggingface_hub.snapshot_download with allow_patterns for minimal required .pt files."
        ),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_manifest_path": str(model_manifest_path),
        "dataset_manifest_path": str(dataset_manifest_path),
    },
    "failure_category": "unknown",
    "error_excerpt": "",
}

safe_write_json(results_path, payload)
PY
