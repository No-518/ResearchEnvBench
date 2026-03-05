#!/usr/bin/env bash
set -u
set -o pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model) into:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Optional:
  --python <path>        Override python executable (highest priority)
  --report-path <path>   Override report.json path (default: /opt/scimlopsbench/report.json)
  --dataset-repo <id>    HF dataset repo id (default: Yuanze/Olympus)
  --dataset-rev <rev>    HF dataset revision (default: main)
  --model-repo <id>      HF model repo id (default: zhumj34/Mipha-3B)
  --model-rev <rev>      HF model revision (default: main)

Auth:
  If a repo requires auth, set HF_TOKEN or HUGGINGFACE_HUB_TOKEN and re-run.
EOF
}

python_override=""
report_path=""
dataset_repo="Yuanze/Olympus"
dataset_rev="main"
# Default training base model per README "Download the Mipha-3B model for fine-tuning".
model_repo="zhumj34/Mipha-3B"
model_rev="main"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_override="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --dataset-repo) dataset_repo="${2:-}"; shift 2 ;;
    --dataset-rev) dataset_rev="${2:-}"; shift 2 ;;
    --model-repo) model_repo="${2:-}"; shift 2 ;;
    --model-rev) model_rev="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

out_dir="$repo_root/build_output/prepare"
log_file="$out_dir/log.txt"
results_json="$out_dir/results.json"

mkdir -p "$out_dir"
: > "$log_file"
exec > >(tee -a "$log_file") 2>&1

runner_py=""
if command -v python3 >/dev/null 2>&1; then runner_py="python3"; fi
if [[ -z "$runner_py" ]] && command -v python >/dev/null 2>&1; then runner_py="python"; fi

status="success"
exit_code=0
failure_category="unknown"
skip_reason="not_applicable"
command_str=""
decision_reason="Download dataset/model from HuggingFace per repository README, store under benchmark_assets/, and derive a 1-sample minimal training dataset."

if [[ -z "$runner_py" ]]; then
  status="failure"
  exit_code=1
  failure_category="deps"
  echo "[prepare] No python/python3 found in PATH to run runner/asset prep." >&2
fi

resolved_python=""
if [[ "$status" == "success" ]]; then
  cmd=( "$runner_py" "$repo_root/benchmark_scripts/runner.py" --stage prepare --task download --requires-python --print-resolved-python )
  [[ -n "$python_override" ]] && cmd+=( --python "$python_override" )
  [[ -n "$report_path" ]] && cmd+=( --report-path "$report_path" )
  command_str="${cmd[*]}"
  echo "[prepare] Resolving python via: $command_str"
  if ! resolved_python="$("${cmd[@]}" 2>/dev/null)"; then
    status="failure"
    exit_code=1
    failure_category="missing_report"
    echo "[prepare] Failed to resolve python from report (or override)." >&2
  fi
fi

cache_root="$repo_root/benchmark_assets/cache"
dataset_dir="$repo_root/benchmark_assets/dataset"
model_dir="$repo_root/benchmark_assets/model"
manifest_path="$repo_root/benchmark_assets/manifest.json"

mkdir -p "$cache_root" "$dataset_dir" "$model_dir"

export PIP_CACHE_DIR="$cache_root/pip_cache"
mkdir -p "$PIP_CACHE_DIR"

export XDG_CACHE_HOME="$cache_root/xdg_cache"
export XDG_CONFIG_HOME="$cache_root/xdg_config"
export XDG_DATA_HOME="$cache_root/xdg_data"
mkdir -p "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"

export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$repo_root/build_output/prepare/pycache"
mkdir -p "$PYTHONPYCACHEPREFIX"

export HF_HOME="$cache_root/hf_home"
export TRANSFORMERS_CACHE="$cache_root/transformers_cache"
export HF_DATASETS_CACHE="$cache_root/hf_datasets_cache"
export TORCH_HOME="$cache_root/torch_cache"
export TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"
export TMPDIR="$repo_root/build_output/prepare/tmp"
mkdir -p "$TMPDIR"

if [[ "$status" == "success" ]]; then
  echo "[prepare] Using python: $resolved_python"
  echo "[prepare] dataset_repo=$dataset_repo dataset_rev=$dataset_rev"
  echo "[prepare] model_repo=$model_repo model_rev=$model_rev"

  PREPARE_OUT_DIR="$out_dir" PREPARE_MANIFEST_PATH="$manifest_path" \
  PREPARE_CACHE_ROOT="$cache_root" PREPARE_DATASET_DIR="$dataset_dir" PREPARE_MODEL_DIR="$model_dir" \
  PREPARE_DATASET_REPO="$dataset_repo" PREPARE_DATASET_REV="$dataset_rev" \
  PREPARE_MODEL_REPO="$model_repo" PREPARE_MODEL_REV="$model_rev" \
  PREPARE_DECISION_REASON="$decision_reason" PREPARE_REPORT_PATH="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}" \
    "$resolved_python" - <<'PY'
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

OUT_DIR = Path(os.environ["PREPARE_OUT_DIR"]).resolve()
MANIFEST_PATH = Path(os.environ["PREPARE_MANIFEST_PATH"]).resolve()
CACHE_ROOT = Path(os.environ["PREPARE_CACHE_ROOT"]).resolve()
DATASET_DIR = Path(os.environ["PREPARE_DATASET_DIR"]).resolve()
MODEL_DIR = Path(os.environ["PREPARE_MODEL_DIR"]).resolve()

DATASET_REPO = os.environ.get("PREPARE_DATASET_REPO", "Yuanze/Olympus")
DATASET_REV = os.environ.get("PREPARE_DATASET_REV", "main")
MODEL_REPO = os.environ.get("PREPARE_MODEL_REPO", "Yuanze/Olympus")
MODEL_REV = os.environ.get("PREPARE_MODEL_REV", "main")

DECISION_REASON = os.environ.get("PREPARE_DECISION_REASON", "")
REPORT_PATH = os.environ.get("PREPARE_REPORT_PATH", "/opt/scimlopsbench/report.json")

RESULTS_PATH = OUT_DIR / "results.json"

def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def git_commit(repo_root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, timeout=5)
            .strip()
        )
    except Exception:
        return ""

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def sha256_dir(root: Path) -> str:
    h = hashlib.sha256()
    files = [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()]
    for p in sorted(files, key=lambda x: x.as_posix()):
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode("utf-8") + b"\0")
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()

def safe_read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json: {path}: {e}"
    except Exception as e:
        return None, f"failed reading json: {path}: {e}"

def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def rel_to_repo(repo_root: Path, p: Path) -> str:
    try:
        return str(p.resolve().relative_to(repo_root.resolve()))
    except Exception:
        return str(p)

def classify_hf_error(msg: str) -> str:
    lower = msg.lower()
    if "401" in lower or "403" in lower or "permission" in lower or "authentication" in lower or "token" in lower:
        return "auth_required"
    if "connection" in lower or "timed out" in lower or "name resolution" in lower or "network" in lower:
        return "download_failed"
    return "download_failed"

repo_root = Path.cwd().resolve()

assets = {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
}

meta: Dict[str, object] = {
    "python": sys.executable,
    "git_commit": git_commit(repo_root),
    "env_vars": {k: os.environ.get(k, "") for k in ["HF_HOME", "HF_DATASETS_CACHE", "TRANSFORMERS_CACHE", "TORCH_HOME"]},
    "decision_reason": DECISION_REASON,
    "timestamp_utc": utc_ts(),
    "report_path": REPORT_PATH,
}

status = "success"
failure_category = "unknown"
error_excerpt = ""

DATASET_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)

minimal_dataset_path = DATASET_DIR / "minimal_train.json"
downloaded_dataset_cache_dir = CACHE_ROOT / "hf_downloads" / "datasets" / DATASET_REPO.replace("/", "__")
downloaded_model_cache_dir = CACHE_ROOT / "hf_downloads" / "models" / MODEL_REPO.replace("/", "__")

def should_reuse_existing() -> bool:
    manifest, _ = safe_read_json(MANIFEST_PATH)
    if not isinstance(manifest, dict):
        return False
    ds = manifest.get("dataset") if isinstance(manifest.get("dataset"), dict) else {}
    md = manifest.get("model") if isinstance(manifest.get("model"), dict) else {}
    ds_path = Path(repo_root / ds.get("path", "")) if ds.get("path") else None
    md_path = Path(repo_root / md.get("path", "")) if md.get("path") else None
    if not ds_path or not md_path:
        return False
    if not ds_path.exists() or not md_path.exists():
        return False
    try:
        if ds.get("sha256") and sha256_file(ds_path) != ds["sha256"]:
            return False
    except Exception:
        return False
    try:
        if md.get("sha256") and sha256_dir(md_path) != md["sha256"]:
            return False
    except Exception:
        return False
    return True

try:
    if should_reuse_existing():
        meta["cache_hit"] = True
        manifest, _ = safe_read_json(MANIFEST_PATH)
        if isinstance(manifest, dict):
            assets["dataset"] = manifest.get("dataset", assets["dataset"])
            assets["model"] = manifest.get("model", assets["model"])
        write_json(
            RESULTS_PATH,
            {
                "status": "success",
                "skip_reason": "not_applicable",
                "exit_code": 0,
                "stage": "prepare",
                "task": "download",
                "command": "reuse benchmark_assets/manifest.json (sha256 match)",
                "timeout_sec": 1200,
                "framework": "unknown",
                "assets": assets,
                "meta": meta,
                "failure_category": "not_applicable",
                "error_excerpt": "",
            },
        )
        sys.exit(0)

    # Ensure huggingface_hub is available (install if missing).
    try:
        import huggingface_hub  # noqa: F401
        from huggingface_hub import snapshot_download
    except Exception as e:
        meta["deps_install_attempted"] = True
        meta["deps_install_cmd"] = f"{sys.executable} -m pip install -q huggingface_hub"
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"])
            from huggingface_hub import snapshot_download  # type: ignore
        except Exception as e2:
            status = "failure"
            failure_category = "deps"
            error_excerpt = f"Failed to import/install huggingface_hub: {e} / {e2}"
            raise

    # Download dataset file Olympus.json into cache, then derive a minimal 1-sample dataset.
    ds_source = f"hf://datasets/{DATASET_REPO}@{DATASET_REV}"
    ds_cache_dir = downloaded_dataset_cache_dir
    ds_cache_dir.mkdir(parents=True, exist_ok=True)

    def download_dataset(local_files_only: bool) -> Path:
        snapshot_download(
            repo_id=DATASET_REPO,
            repo_type="dataset",
            revision=DATASET_REV,
            local_dir=str(ds_cache_dir),
            allow_patterns=["Olympus.json"],
            local_dir_use_symlinks=False,
            local_files_only=local_files_only,
        )
        candidate = ds_cache_dir / "Olympus.json"
        if not candidate.exists():
            # Some hub layouts place files in subfolders; search only under cache dir.
            matches = list(ds_cache_dir.rglob("Olympus.json"))
            if matches:
                return matches[0]
            raise FileNotFoundError(f"Olympus.json not found under {ds_cache_dir}")
        return candidate

    try:
        ds_file = download_dataset(local_files_only=False)
        meta["dataset_download_mode"] = "online_or_cache"
    except Exception as e:
        meta["dataset_download_error"] = str(e)
        try:
            ds_file = download_dataset(local_files_only=True)
            meta["dataset_download_mode"] = "offline_cache"
        except Exception as e2:
            status = "failure"
            failure_category = classify_hf_error(str(e))
            error_excerpt = f"Dataset download failed (offline cache also missing): {e2}"
            if failure_category == "auth_required":
                error_excerpt += "\nHint: export HF_TOKEN or HUGGINGFACE_HUB_TOKEN and re-run."
            raise

    # Build minimal dataset for one-step training: take the first example and strip image.
    try:
        raw = json.loads(ds_file.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            raise ValueError("Olympus.json is not a non-empty list")
        ex = raw[0]
        if not isinstance(ex, dict):
            raise ValueError("First item is not an object")
        ex = dict(ex)
        ex.pop("image", None)
        # Keep only first 2 turns if available.
        conv = ex.get("conversations")
        if isinstance(conv, list) and len(conv) > 2:
            ex["conversations"] = conv[:2]
        minimal_dataset_path.write_text(json.dumps([ex], ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        status = "failure"
        failure_category = "data"
        error_excerpt = f"Failed to construct minimal dataset from {ds_file}: {e}"
        raise

    # Download model snapshot into cache, then link into benchmark_assets/model.
    model_source = f"hf://models/{MODEL_REPO}@{MODEL_REV}"
    md_cache_dir = downloaded_model_cache_dir
    md_cache_dir.mkdir(parents=True, exist_ok=True)

    def download_model(local_files_only: bool) -> Path:
        local_path = snapshot_download(
            repo_id=MODEL_REPO,
            revision=MODEL_REV,
            local_dir=str(md_cache_dir),
            local_dir_use_symlinks=False,
            local_files_only=local_files_only,
        )
        p = Path(local_path)
        if not p.exists():
            raise FileNotFoundError(f"snapshot_download reported {local_path} but it does not exist")
        return p

    try:
        model_cache_path = download_model(local_files_only=False)
        meta["model_download_mode"] = "online_or_cache"
    except Exception as e:
        meta["model_download_error"] = str(e)
        try:
            model_cache_path = download_model(local_files_only=True)
            meta["model_download_mode"] = "offline_cache"
        except Exception as e2:
            status = "failure"
            failure_category = classify_hf_error(str(e))
            error_excerpt = f"Model download failed (offline cache also missing): {e2}"
            if failure_category == "auth_required":
                error_excerpt += "\nHint: export HF_TOKEN or HUGGINGFACE_HUB_TOKEN and re-run."
            raise

    # Resolve/link model directory without assuming any external hub cache layout.
    resolved_model_dir = model_cache_path
    if not resolved_model_dir.exists():
        status = "failure"
        failure_category = "model"
        error_excerpt = f"Resolved model directory does not exist: {resolved_model_dir}"
        raise FileNotFoundError(error_excerpt)

    model_link_path = MODEL_DIR / "model"
    if model_link_path.exists() or model_link_path.is_symlink():
        try:
            if model_link_path.is_symlink() or model_link_path.is_dir():
                if model_link_path.is_symlink() and model_link_path.resolve() == resolved_model_dir.resolve():
                    pass
                else:
                    if model_link_path.is_dir() and not model_link_path.is_symlink():
                        shutil.rmtree(model_link_path)
                    else:
                        model_link_path.unlink()
        except Exception:
            pass
    try:
        model_link_path.symlink_to(resolved_model_dir, target_is_directory=True)
        meta["model_link"] = "symlink"
    except Exception:
        # Fall back to copy (expensive).
        if model_link_path.exists():
            shutil.rmtree(model_link_path, ignore_errors=True)
        shutil.copytree(resolved_model_dir, model_link_path)
        meta["model_link"] = "copy"

    # Compute sha256 for prepared assets (used paths).
    ds_sha = sha256_file(minimal_dataset_path)
    md_sha = sha256_dir(model_link_path)

    assets = {
        "dataset": {
            "path": rel_to_repo(repo_root, minimal_dataset_path),
            "source": ds_source,
            "version": DATASET_REV,
            "sha256": ds_sha,
        },
        "model": {
            "path": rel_to_repo(repo_root, model_link_path),
            "source": model_source,
            "version": MODEL_REV,
            "sha256": md_sha,
        },
    }
    manifest_payload = {
        "dataset": {
            **assets["dataset"],
            "downloaded_path": rel_to_repo(repo_root, ds_file),
            "downloaded_sha256": sha256_file(ds_file),
        },
        "model": {
            **assets["model"],
            "downloaded_path": rel_to_repo(repo_root, resolved_model_dir),
        },
        "meta": {
            "timestamp_utc": utc_ts(),
            "notes": "dataset.path points to a 1-example subset derived from downloaded Olympus.json with image removed",
        },
    }
    write_json(MANIFEST_PATH, manifest_payload)

    write_json(
        RESULTS_PATH,
        {
            "status": "success",
            "skip_reason": "not_applicable",
            "exit_code": 0,
            "stage": "prepare",
            "task": "download",
            "command": "huggingface_hub.snapshot_download (dataset + model)",
            "timeout_sec": 1200,
            "framework": "unknown",
            "assets": assets,
            "meta": meta,
            "failure_category": "not_applicable",
            "error_excerpt": "",
        },
    )
    sys.exit(0)
except Exception as e:
    if status != "failure":
        status = "failure"
        failure_category = "unknown"
        error_excerpt = str(e)
    write_json(
        RESULTS_PATH,
        {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "prepare",
            "task": "download",
            "command": "huggingface_hub.snapshot_download (dataset + model)",
            "timeout_sec": 1200,
            "framework": "unknown",
            "assets": assets,
            "meta": meta,
            "failure_category": failure_category,
            "error_excerpt": error_excerpt or str(e),
        },
    )
    sys.exit(1)
PY
  stage_rc=$?
  if [[ $stage_rc -ne 0 ]]; then
    status="failure"
    exit_code=1
    # Prefer the failure_category already written by the python stage.
    if [[ -f "$results_json" ]]; then
      : # keep
    else
      failure_category="download_failed"
    fi
  fi
fi

# Ensure results.json exists even if python couldn't start.
if [[ ! -f "$results_json" ]]; then
  cat >"$results_json" <<EOF
{
  "status": "${status}",
  "skip_reason": "${skip_reason}",
  "exit_code": ${exit_code},
  "stage": "prepare",
  "task": "download",
  "command": "${command_str}",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "${resolved_python}",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "${decision_reason}"
  },
  "failure_category": "${failure_category}",
  "error_excerpt": "prepare_assets.sh failed before python stage could write results.json"
}
EOF
fi

exit "$exit_code"
