#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model) into benchmark_assets/ and write build_output/prepare/results.json.

Outputs (fixed):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Default assets (from official examples):
  dataset: finetrainers/crush-smol
  model:   Wan-AI/Wan2.1-T2V-1.3B-Diffusers (model_name=wan)

Overrides:
  --report-path <path>     Agent report JSON (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  --python <path>          Explicit python to use (overrides report)
  --out-dir <path>         Root output dir (default: build_output)

Env overrides (optional):
  BENCH_DATASET_ID
  BENCH_DATASET_REVISION
  BENCH_MODEL_NAME
  BENCH_MODEL_ID
  BENCH_MODEL_REVISION

Notes:
  - Downloads go to benchmark_assets/cache/, and resolved paths are linked into:
      benchmark_assets/dataset/current
      benchmark_assets/model/current
  - If offline and cache exists, reuses cached snapshots.
  - If assets are gated, set HF_TOKEN or HUGGINGFACE_HUB_TOKEN and re-run.
EOF
}

report_path=""
python_override=""
out_root="build_output"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --python)
      python_override="${2:-}"; shift 2 ;;
    --out-dir)
      out_root="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_root_abs="$(cd "$repo_root" && mkdir -p "$out_root" && cd "$out_root" && pwd)"
stage_dir="$out_root_abs/prepare"
mkdir -p "$stage_dir"

log_path="$stage_dir/log.txt"
results_json="$stage_dir/results.json"
exec > >(tee "$log_path") 2>&1

sys_python="$(command -v python3 || command -v python || true)"
if [[ -z "$sys_python" ]]; then
  cat >"$results_json" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "python (not found)",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "No python interpreter found on PATH to run prepare stage."
  },
  "failure_category": "deps",
  "error_excerpt": ""
}
JSON
  exit 1
fi

resolve_python_args=()
[[ -n "$report_path" ]] && resolve_python_args+=(--report-path "$report_path")
[[ -n "$python_override" ]] && resolve_python_args+=(--python "$python_override")

echo "[prepare_assets] resolving python via benchmark_scripts/runner.py ${resolve_python_args[*]}"
if ! resolved_python="$("$sys_python" benchmark_scripts/runner.py --print-python "${resolve_python_args[@]}")"; then
  echo "[prepare_assets] failed to resolve python via report; provide --python or ensure /opt/scimlopsbench/report.json is present." >&2
  "$sys_python" - <<PY || true
import json, os, subprocess
from pathlib import Path
repo_root = Path(${repo_root@Q})
stage_dir = Path(${stage_dir@Q})
log_path = stage_dir / "log.txt"
def git_commit():
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"prepare",
  "task":"download",
  "command":"resolve_python(report.json)",
  "timeout_sec":1200,
  "framework":"unknown",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{
    "python":"",
    "git_commit": git_commit(),
    "env_vars": {k:os.environ.get(k) for k in ["SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON"] if os.environ.get(k)},
    "decision_reason":"missing/invalid agent report; cannot resolve python_path"
  },
  "failure_category":"missing_report",
  "error_excerpt":"\\n".join(log_path.read_text(errors='replace').splitlines()[-220:]) if log_path.exists() else ""
}
(stage_dir / "results.json").write_text(json.dumps(payload, indent=2) + "\\n")
PY
  exit 1
fi

echo "[prepare_assets] resolved_python=$resolved_python"

# Constrain all caches/writes to new directories only.
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HOME="$repo_root/benchmark_assets/cache/hf_home"
export HF_HUB_CACHE="$HF_HOME/hub"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export HF_DATASETS_CACHE="$HF_HOME/datasets"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export WANDB_MODE="${WANDB_MODE:-offline}"
export WANDB_DIR="$stage_dir/wandb"

STAGE_DIR="$stage_dir" OUT_ROOT="$out_root_abs" REPO_ROOT="$repo_root" RESOLVED_PYTHON="$resolved_python" \
  "$resolved_python" - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

stage_dir = Path(os.environ["STAGE_DIR"]).resolve()
repo_root = Path(os.environ["REPO_ROOT"]).resolve()
resolved_python = os.environ.get("RESOLVED_PYTHON", "")
out_root = Path(os.environ["OUT_ROOT"]).resolve()

results_path = stage_dir / "results.json"
log_path = stage_dir / "log.txt"

assets_root = repo_root / "benchmark_assets"
cache_root = assets_root / "cache"
dataset_link = assets_root / "dataset" / "current"
model_link = assets_root / "model" / "current"
manifest_path = assets_root / "manifest.json"
dataset_config_path = assets_root / "dataset" / "dataset_config.json"

hf_cache_dir = cache_root / "hf"

DEFAULT_DATASET_ID = "finetrainers/crush-smol"
DEFAULT_MODEL_NAME = "wan"
DEFAULT_MODEL_ID = "Wan-AI/Wan2.1-T2V-1.3B-Diffusers"

dataset_id = os.environ.get("BENCH_DATASET_ID", DEFAULT_DATASET_ID)
dataset_revision = os.environ.get("BENCH_DATASET_REVISION") or None
model_name_override = os.environ.get("BENCH_MODEL_NAME") or None
model_id_override = os.environ.get("BENCH_MODEL_ID") or None
model_revision_override = os.environ.get("BENCH_MODEL_REVISION") or None

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""

def tail_log(max_lines: int = 220) -> str:
    try:
        return "\n".join(log_path.read_text(errors="replace").splitlines()[-max_lines:])
    except Exception:
        return ""

def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def version_from_snapshot_path(p: Path) -> str:
    # snapshot_download typically returns .../snapshots/<commit_hash>
    name = p.name
    if re.fullmatch(r"[0-9a-f]{8,64}", name):
        return name
    return ""

def safe_symlink(link_path: Path, target: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_symlink():
            link_path.unlink()
        else:
            raise RuntimeError(f"Refusing to overwrite non-symlink path: {link_path}")
    link_path.symlink_to(target, target_is_directory=True)

def write_results(
    *,
    status: str,
    exit_code: int,
    failure_category: str,
    command: str,
    assets: Dict[str, Any],
    meta: Dict[str, Any],
    error_excerpt: str,
) -> None:
    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "prepare",
        "task": "download",
        "command": command,
        "timeout_sec": 1200,
        "framework": "unknown",
        "assets": assets,
        "meta": meta,
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

assets_empty = {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
}

meta_base = {
    "python": resolved_python,
    "git_commit": git_commit(),
    "env_vars": {
        "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
        "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        "HF_HOME": os.environ.get("HF_HOME", ""),
        "HF_HUB_CACHE": os.environ.get("HF_HUB_CACHE", ""),
        "HF_DATASETS_CACHE": os.environ.get("HF_DATASETS_CACHE", ""),
        "TRANSFORMERS_CACHE": os.environ.get("TRANSFORMERS_CACHE", ""),
        "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", ""),
    },
    "timestamp_utc": utc_now_iso(),
    "decision_reason": "Selected default assets from finetrainers official examples; override via BENCH_* env vars if needed.",
}

def _tail(s: str, n: int = 2000) -> str:
    return s[-n:] if s else ""

# For the default video dataset, training/inference may require `torchcodec` + FFmpeg shared libs.
# Per benchmark policy, we only *detect* and record the state here (no auto-install / no environment mutation).
torchcodec_info: Dict[str, Any] = {"import_ok": False, "version": "", "error": ""}
try:
    import torchcodec  # type: ignore

    torchcodec_info["import_ok"] = True
    torchcodec_info["version"] = getattr(torchcodec, "__version__", "")
except Exception as e:
    torchcodec_info["error"] = _tail(str(e), 6000)

meta_base["torchcodec"] = torchcodec_info

def load_existing_manifest() -> Optional[Dict[str, Any]]:
    try:
        if not manifest_path.exists():
            return None
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None

def manifest_paths_ok(manifest: Dict[str, Any]) -> bool:
    try:
        ds_link = (repo_root / (manifest.get("paths") or {}).get("dataset_symlink", "")).resolve()
        mdl_link = (repo_root / (manifest.get("paths") or {}).get("model_symlink", "")).resolve()
        cfg = (repo_root / (manifest.get("paths") or {}).get("dataset_config", "")).resolve()
        return ds_link.exists() and mdl_link.exists() and cfg.exists()
    except Exception:
        return False

existing = load_existing_manifest()
if existing and manifest_paths_ok(existing):
    ds = existing.get("dataset") or {}
    mdl = existing.get("model") or {}
    assets = {
        "dataset": {
            "path": str(repo_root / (existing.get("paths") or {}).get("dataset_symlink", "benchmark_assets/dataset/current")),
            "source": str(ds.get("source") or ""),
            "version": str(ds.get("version") or ds.get("revision") or ""),
            "sha256": str(ds.get("sha256") or ""),
        },
        "model": {
            "path": str(repo_root / (existing.get("paths") or {}).get("model_symlink", "benchmark_assets/model/current")),
            "source": str(mdl.get("source") or ""),
            "version": str(mdl.get("version") or mdl.get("revision") or ""),
            "sha256": str(mdl.get("sha256") or ""),
        },
    }
    meta = dict(meta_base)
    meta["reused_cached_assets"] = True
    meta["manifest_path"] = str(manifest_path)
    meta["dataset_id"] = str(ds.get("id") or dataset_id)
    meta["model_id"] = str(mdl.get("id") or model_id_override or DEFAULT_MODEL_ID)
    meta["model_name"] = str(existing.get("model_name") or model_name_override or DEFAULT_MODEL_NAME)
    write_results(
        status="success",
        exit_code=0,
        failure_category="",
        command="reuse(manifest.json)",
        assets=assets,
        meta=meta,
        error_excerpt="",
    )
    raise SystemExit(0)

# Fresh download / resolve
try:
    try:
        from huggingface_hub import snapshot_download
    except Exception as e:
        meta = dict(meta_base)
        meta["decision_reason"] = "huggingface_hub import failed; cannot download assets."
        write_results(
            status="failure",
            exit_code=1,
            failure_category="deps",
            command="import huggingface_hub.snapshot_download",
            assets=assets_empty,
            meta=meta,
            error_excerpt=str(e),
        )
        raise SystemExit(1)

    hf_cache_dir.mkdir(parents=True, exist_ok=True)

    def download_snapshot(
        *,
        repo_id: str,
        repo_type: str,
        revision: Optional[str],
        local_only: bool,
    ) -> Tuple[Optional[Path], Optional[str], Optional[str]]:
        try:
            p = snapshot_download(
                repo_id=repo_id,
                repo_type=repo_type,
                revision=revision,
                cache_dir=str(hf_cache_dir),
                local_files_only=local_only,
            )
            path = Path(p).resolve()
            version = version_from_snapshot_path(path) or (revision or "")
            sha = sha256_str(version) if version else ""
            return path, version, sha
        except Exception as e:
            return None, None, str(e)

    # 1) Dataset: prefer local cache, then network.
    dataset_path, dataset_version, dataset_sha = download_snapshot(
        repo_id=dataset_id, repo_type="dataset", revision=dataset_revision, local_only=True
    )
    dataset_download_mode = "local_only"
    dataset_err = None
    if dataset_path is None:
        dataset_path, dataset_version, dataset_sha = download_snapshot(
            repo_id=dataset_id, repo_type="dataset", revision=dataset_revision, local_only=False
        )
        dataset_download_mode = "network"
    if dataset_path is None:
        dataset_err = dataset_sha or "unknown dataset download error"
        is_auth = bool(re.search(r"(401|403|gated|authorization|unauthorized|forbidden)", dataset_err, re.I))
        cat = "auth_required" if is_auth else "download_failed"
        meta = dict(meta_base)
        meta["dataset_id"] = dataset_id
        meta["dataset_revision"] = dataset_revision
        meta["dataset_download_mode_attempted"] = dataset_download_mode
        if is_auth and not (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")):
            meta["decision_reason"] = (
                f"Dataset {dataset_id} appears to require authentication; set HF_TOKEN/HUGGINGFACE_HUB_TOKEN."
            )
        write_results(
            status="failure",
            exit_code=1,
            failure_category=cat,
            command=f"snapshot_download(repo_type=dataset, repo_id={dataset_id})",
            assets=assets_empty,
            meta=meta,
            error_excerpt=dataset_err,
        )
        raise SystemExit(1)

    # 2) Model: try an ordered list of candidates, prefer overrides.
    candidates = []
    if model_name_override or model_id_override:
        candidates.append((model_name_override or DEFAULT_MODEL_NAME, model_id_override or DEFAULT_MODEL_ID, model_revision_override))
    candidates.extend(
        [
            (DEFAULT_MODEL_NAME, DEFAULT_MODEL_ID, None),
            ("ltx_video", "a-r-r-o-w/LTX-Video-diffusers", None),
        ]
    )

    model_path = None
    model_version = ""
    model_sha = ""
    model_id = ""
    model_name = ""
    model_revision = None
    model_error_items = []

    for cand_model_name, cand_model_id, cand_revision in candidates:
        # Avoid duplicates
        if model_id and cand_model_id == model_id:
            continue
        model_name = cand_model_name
        model_id = cand_model_id
        model_revision = cand_revision

        model_path, model_version, model_sha = download_snapshot(
            repo_id=model_id, repo_type="model", revision=model_revision, local_only=True
        )
        model_download_mode = "local_only"
        err = None
        if model_path is None:
            model_path, model_version, model_sha = download_snapshot(
                repo_id=model_id, repo_type="model", revision=model_revision, local_only=False
            )
            model_download_mode = "network"
        if model_path is not None:
            break
        err = model_sha or "unknown model download error"
        model_error_items.append({"model_id": model_id, "error": err})

    if model_path is None:
        combined = "\n".join(f'{i["model_id"]}: {i["error"]}' for i in model_error_items)
        is_auth = bool(re.search(r"(401|403|gated|authorization|unauthorized|forbidden)", combined, re.I))
        cat = "auth_required" if is_auth else "download_failed"
        meta = dict(meta_base)
        meta["dataset_id"] = dataset_id
        meta["model_candidates_tried"] = [x[1] for x in candidates]
        meta["decision_reason"] = (
            "All candidate models failed to download; if gated, set HF_TOKEN/HUGGINGFACE_HUB_TOKEN."
        )
        write_results(
            status="failure",
            exit_code=1,
            failure_category=cat,
            command="snapshot_download(repo_type=model, candidates=...)",
            assets=assets_empty,
            meta=meta,
            error_excerpt=combined,
        )
        raise SystemExit(1)

    # Validate resolved dirs exist (robust path resolution requirement).
    if not dataset_path.exists():
        meta = dict(meta_base)
        meta["decision_reason"] = "Downloader reported success but dataset path does not exist."
        write_results(
            status="failure",
            exit_code=1,
            failure_category="data",
            command=f"snapshot_download(repo_type=dataset, repo_id={dataset_id})",
            assets=assets_empty,
            meta=meta,
            error_excerpt=f"resolved dataset path missing: {dataset_path}",
        )
        raise SystemExit(1)
    if not model_path.exists():
        meta = dict(meta_base)
        meta["decision_reason"] = "Downloader reported success but model path does not exist."
        write_results(
            status="failure",
            exit_code=1,
            failure_category="model",
            command=f"snapshot_download(repo_type=model, repo_id={model_id})",
            assets=assets_empty,
            meta=meta,
            error_excerpt=f"resolved model path missing: {model_path}",
        )
        raise SystemExit(1)

    # Link into benchmark_assets/{dataset,model}/current
    safe_symlink(dataset_link, dataset_path)
    safe_symlink(model_link, model_path)

    # Dataset config for training stages (local data_root).
    dataset_config_path.parent.mkdir(parents=True, exist_ok=True)
    dataset_config = {
        "datasets": [
            {
                "data_root": str(dataset_link),
                "dataset_type": "video",
                "id_token": "PIKA_CRUSH",
                "video_resolution_buckets": [[49, 480, 832]],
                "reshape_mode": "bicubic",
                "remove_common_llm_caption_prefixes": True,
            }
        ]
    }
    dataset_config_path.write_text(json.dumps(dataset_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Write manifest for downstream stages.
    manifest = {
        "created_utc": utc_now_iso(),
        "model_name": model_name,
        "training_type": "lora",
        "deps": {
            "torchcodec": {
                "import_ok": bool(torchcodec_info.get("import_ok")),
                "version": str(torchcodec_info.get("version") or ""),
                "error": str(torchcodec_info.get("error") or ""),
                # Kept for backward compatibility with older stage scripts; we don't provision FFmpeg.
                "ld_library_path_prefix": "",
            }
        },
        "dataset": {
            "id": dataset_id,
            "revision": dataset_revision,
            "resolved_path": str(dataset_path),
            "version": str(dataset_version or ""),
            "sha256": str(dataset_sha or ""),
            "source": "huggingface_hub.snapshot_download",
        },
        "model": {
            "id": model_id,
            "revision": model_revision,
            "resolved_path": str(model_path),
            "version": str(model_version or ""),
            "sha256": str(model_sha or ""),
            "source": "huggingface_hub.snapshot_download",
        },
        "paths": {
            "dataset_symlink": "benchmark_assets/dataset/current",
            "model_symlink": "benchmark_assets/model/current",
            "dataset_config": "benchmark_assets/dataset/dataset_config.json",
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assets = {
        "dataset": {"path": str(dataset_link), "source": manifest["dataset"]["source"], "version": dataset_version, "sha256": dataset_sha},
        "model": {"path": str(model_link), "source": manifest["model"]["source"], "version": model_version, "sha256": model_sha},
    }
    meta = dict(meta_base)
    meta.update(
        {
            "reused_cached_assets": False,
            "manifest_path": str(manifest_path),
            "dataset_id": dataset_id,
            "dataset_revision": dataset_revision,
            "model_name": model_name,
            "model_id": model_id,
            "model_revision": model_revision,
            "hf_cache_dir": str(hf_cache_dir),
        }
    )

    write_results(
        status="success",
        exit_code=0,
        failure_category="",
        command=f"snapshot_download(dataset={dataset_id}, model={model_id})",
        assets=assets,
        meta=meta,
        error_excerpt="",
    )
    raise SystemExit(0)

except SystemExit:
    raise
except Exception as e:
    meta = dict(meta_base)
    meta["decision_reason"] = "Unhandled exception in prepare_assets stage."
    meta["exception"] = repr(e)
    meta["traceback"] = traceback.format_exc()
    write_results(
        status="failure",
        exit_code=1,
        failure_category="unknown",
        command="prepare_assets(unhandled)",
        assets=assets_empty,
        meta=meta,
        error_excerpt=tail_log(),
    )
    raise SystemExit(1)
PY

exit_code="$("$sys_python" - <<PY
import json
from pathlib import Path
p=Path(${results_json@Q})
try:
  d=json.loads(p.read_text())
  print(int(d.get("exit_code", 1)))
except Exception:
  print(1)
PY
)"
exit "$exit_code"
