#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model artifacts).

Writes:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Also populates:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/
  benchmark_assets/assets.json        # manifest consumed by run stages

Optional env vars:
  SCIMLOPSBENCH_DATASET_URL           # anonymous URL for a tiny dataset file
  SCIMLOPSBENCH_MODEL_URL             # anonymous URL for a tiny model file
  SCIMLOPSBENCH_REPORT                # report.json path (default: /opt/scimlopsbench/report.json)
  SCIMLOPSBENCH_PYTHON                # override python interpreter
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/prepare"
assets_root="$repo_root/benchmark_assets"
mkdir -p "$stage_dir" "$assets_root/cache" "$assets_root/dataset" "$assets_root/model"

impl_py="$stage_dir/prepare_assets_impl.py"
results_extra="$stage_dir/results_extra.json"
assets_manifest="$assets_root/assets.json"

cat >"$impl_py" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if src.is_file() and dst.is_file() and sha256_file(src) == sha256_file(dst):
            return
    try:
        if dst.exists():
            dst.unlink()
        os.link(src, dst)
    except Exception:
        shutil.copy2(src, dst)


def download_to(url: str, dst: Path, timeout_sec: int = 60) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    req = urllib.request.Request(url, headers={"User-Agent": "scimlopsbench/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as r:
        with tmp.open("wb") as f:
            shutil.copyfileobj(r, f)
    tmp.replace(dst)


def git_commit(repo_root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, stderr=subprocess.DEVNULL)
            .strip()
        )
    except Exception:
        return ""


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    assets_root = repo_root / "benchmark_assets"
    cache_dir = assets_root / "cache"
    dataset_dir = assets_root / "dataset"
    model_dir = assets_root / "model"
    manifest_path = assets_root / "assets.json"
    results_extra_path = repo_root / "build_output" / "prepare" / "results_extra.json"

    dataset_url = os.environ.get("SCIMLOPSBENCH_DATASET_URL", "").strip()
    model_url = os.environ.get("SCIMLOPSBENCH_MODEL_URL", "").strip()

    decision_reason_parts = []

    # Dataset
    dataset_source = ""
    dataset_file: Optional[Path] = None
    dataset_cache_file: Optional[Path] = None

    if dataset_url:
        name = Path(dataset_url.split("?")[0]).name or "dataset.bin"
        dataset_cache_file = cache_dir / name
        dataset_source = dataset_url
        decision_reason_parts.append(f"dataset: url={dataset_url}")
        if not dataset_cache_file.exists():
            try:
                download_to(dataset_url, dataset_cache_file)
            except Exception as e:
                if dataset_cache_file.exists():
                    decision_reason_parts.append(f"dataset: download failed, using cached file: {e}")
                else:
                    raise RuntimeError(f"download_failed: dataset download failed: {e}") from e
    else:
        # Repo has no dataset spec; use a stable local file as minimal dataset artifact.
        local = repo_root / "agorabanner.png"
        if not local.exists():
            raise RuntimeError("data: default dataset fallback agorabanner.png not found in repo root")
        dataset_cache_file = cache_dir / local.name
        dataset_source = "repo_file:agorabanner.png"
        decision_reason_parts.append("dataset: repo provides no dataset; using agorabanner.png as minimal dataset artifact")
        safe_copy(local, dataset_cache_file)

    dataset_dir.mkdir(parents=True, exist_ok=True)
    dataset_file = dataset_dir / dataset_cache_file.name
    safe_copy(dataset_cache_file, dataset_file)
    dataset_sha = sha256_file(dataset_file)

    # Model
    model_source = ""
    model_artifact: Optional[Path] = None
    model_cache_file: Optional[Path] = None

    if model_url:
        name = Path(model_url.split("?")[0]).name or "model.bin"
        model_cache_file = cache_dir / name
        model_source = model_url
        decision_reason_parts.append(f"model: url={model_url}")
        if not model_cache_file.exists():
            try:
                download_to(model_url, model_cache_file)
            except Exception as e:
                if model_cache_file.exists():
                    decision_reason_parts.append(f"model: download failed, using cached file: {e}")
                else:
                    raise RuntimeError(f"download_failed: model download failed: {e}") from e
    else:
        # Repo provides only a model definition; create a minimal config artifact.
        model_cache_file = cache_dir / "model_config.json"
        model_source = "generated:model_config"
        decision_reason_parts.append("model: repo provides no pretrained weights; generating minimal model_config.json")
        model_cfg = {
            "arch": "vision_mamba.Vim",
            "config": {
                "dim": 256,
                "heads": 8,
                "dt_rank": 32,
                "dim_inner": 256,
                "d_state": 256,
                "num_classes": 1000,
                "image_size": 224,
                "patch_size": 16,
                "channels": 3,
                "dropout": 0.1,
                "depth": 12,
            },
        }
        write_json(model_cache_file, model_cfg)

    model_dir.mkdir(parents=True, exist_ok=True)
    model_artifact = model_dir / model_cache_file.name
    safe_copy(model_cache_file, model_artifact)
    model_sha = sha256_file(model_artifact)

    manifest = {
        "dataset": {
            "path": str(dataset_dir),
            "source": dataset_source,
            "version": "",
            "sha256": dataset_sha,
        },
        "model": {
            "path": str(model_dir),
            "source": model_source,
            "version": "",
            "sha256": model_sha,
        },
    }
    write_json(manifest_path, manifest)

    extra = {
        "assets": manifest,
        "meta": {
            "prepare": {
                "git_commit": git_commit(repo_root),
                "decision_reason": "; ".join(decision_reason_parts),
                "dataset_file": str(dataset_file),
                "model_artifact": str(model_artifact),
            }
        },
    }
    write_json(results_extra_path, extra)
    print("[prepare] assets.json written:", manifest_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        # Best-effort: write results_extra.json with failure_category hints.
        try:
            repo_root = Path(__file__).resolve().parents[2]
            results_extra_path = repo_root / "build_output" / "prepare" / "results_extra.json"
            results_extra_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"failure_category": "download_failed" if "download_failed:" in str(e) else "unknown"}
            results_extra_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
        raise
PY

python3_bin="$(command -v python3 || command -v python)"

"$python3_bin" "$repo_root/benchmark_scripts/runner.py" \
  --stage prepare \
  --task download \
  --out-dir "$stage_dir" \
  --timeout-sec 1200 \
  --framework unknown \
  --requires-python \
  --failure-category download_failed \
  --results-extra-json "$results_extra" \
  --python-script "$impl_py"

