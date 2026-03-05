#!/usr/bin/env bash
set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model/config).

This benchmark uses the repo's built-in PyTorch Lightning training entrypoint:
  auto1111sdk/modules/generative/main.py
with the included toy config:
  auto1111sdk/modules/generative/configs/example_training/toy/mnist.yaml

Dataset: MNIST (public, anonymous download).
Model:   The toy MNIST training config copied into benchmark_assets/model/.

Outputs (always written, even on failure):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Assets written under:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Options:
  --python <path>        Override python (bypasses report.json)
  --report-path <path>  Override report.json path (default: /opt/scimlopsbench/report.json)
  --timeout-sec <sec>   Default: 1200
EOF
}

python_override=""
report_path=""
timeout_sec="1200"
py_runner="python"

if ! command -v "$py_runner" >/dev/null 2>&1; then
  py_runner="python3"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_override="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --timeout-sec)
      timeout_sec="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="$repo_root/build_output/prepare"
extra_json="$out_dir/extra.json"

mkdir -p "$out_dir"

runner_args=(
  --stage prepare
  --task download
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --out-dir "$out_dir"
  --decision-reason "Selected repo-included Lightning MNIST toy training config (small, public dataset; no auth required)."
  --extra-json "$extra_json"
)
if [[ -n "$python_override" ]]; then
  runner_args+=(--python "$python_override")
fi
if [[ -n "$report_path" ]]; then
  runner_args+=(--report-path "$report_path")
fi

cmd_str="$(cat <<'BASH'
set -euo pipefail

"${SCIMLOPSBENCH_PYTHON_RESOLVED}" - <<'PY'
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("SCIMLOPSBENCH_REPO_ROOT", ".")).resolve()
OUT_DIR = REPO_ROOT / "build_output" / "prepare"
EXTRA_JSON = OUT_DIR / "extra.json"

CACHE_ROOT = REPO_ROOT / "benchmark_assets" / "cache"
DATASET_ROOT = REPO_ROOT / "benchmark_assets" / "dataset"
MODEL_ROOT = REPO_ROOT / "benchmark_assets" / "model"

MNIST_URL_BASE = "https://storage.googleapis.com/cvdf-datasets/mnist/"
MNIST_FILES = [
    "train-images-idx3-ubyte.gz",
    "train-labels-idx1-ubyte.gz",
    "t10k-images-idx3-ubyte.gz",
    "t10k-labels-idx1-ubyte.gz",
]

SRC_MODEL_CONFIG = REPO_ROOT / "auto1111sdk" / "modules" / "generative" / "configs" / "example_training" / "toy" / "mnist.yaml"

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def sha256_json(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def write_extra(payload: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    EXTRA_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def download(url: str, dest: Path, *, timeout_sec: int = 60) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "scimlopsbench/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)

def copytree_overwrite(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

assets = {
    "dataset": {"path": "", "source": "mnist", "version": "mnist", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
}
meta_extra = {}

try:
    if not SRC_MODEL_CONFIG.exists():
        write_extra(
            {
                "assets": assets,
                "failure_category": "model",
                "meta": {"prepare_error": f"missing model config in repo: {SRC_MODEL_CONFIG}"},
            }
        )
        raise SystemExit(1)

    # Dataset layout: training entrypoint downloads to ".data/" relative to cwd.
    # We stage MNIST under: benchmark_assets/dataset/mnist/.data/
    cache_mnist_root = CACHE_ROOT / "mnist" / ".data"
    cache_raw_dir = cache_mnist_root / "MNIST" / "raw"
    cache_manifest_path = cache_mnist_root / "sha256_manifest.json"

    cache_manifest = {}
    if cache_manifest_path.exists():
        try:
            cache_manifest = json.loads(cache_manifest_path.read_text(encoding="utf-8"))
        except Exception:
            cache_manifest = {}

    download_errors = []
    for fname in MNIST_FILES:
        url = MNIST_URL_BASE + fname
        dest = cache_raw_dir / fname
        if dest.exists() and fname in cache_manifest:
            try:
                current = sha256_file(dest)
                if current == cache_manifest[fname]:
                    continue
            except Exception:
                pass
        if dest.exists() and fname not in cache_manifest:
            try:
                cache_manifest[fname] = sha256_file(dest)
                continue
            except Exception:
                pass
        try:
            download(url, dest)
            cache_manifest[fname] = sha256_file(dest)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            if dest.exists():
                # Offline reuse: keep existing file.
                try:
                    cache_manifest[fname] = sha256_file(dest)
                except Exception:
                    pass
                download_errors.append(f"{fname}: download failed but file exists; reusing ({type(e).__name__}: {e})")
            else:
                download_errors.append(f"{fname}: download failed and file missing ({type(e).__name__}: {e})")
        except Exception as e:
            if dest.exists():
                download_errors.append(f"{fname}: unexpected download error but file exists; reusing ({type(e).__name__}: {e})")
            else:
                download_errors.append(f"{fname}: unexpected download error and file missing ({type(e).__name__}: {e})")

    missing = [f for f in MNIST_FILES if not (cache_raw_dir / f).exists()]
    if missing:
        write_extra(
            {
                "assets": assets,
                "failure_category": "download_failed",
                "meta": {
                    "prepare_error": "MNIST download incomplete",
                    "missing_files": missing,
                    "download_errors": download_errors,
                    "cache_root": str(cache_mnist_root),
                },
            }
        )
        raise SystemExit(1)

    cache_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    cache_manifest_path.write_text(json.dumps(cache_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    dataset_sha = sha256_json(cache_manifest)
    final_dataset_dir = DATASET_ROOT / "mnist"
    final_data_root = final_dataset_dir / ".data"
    copytree_overwrite(cache_mnist_root, final_data_root)

    assets["dataset"] = {
        "path": str(final_dataset_dir),
        "source": MNIST_URL_BASE,
        "version": "mnist",
        "sha256": dataset_sha,
    }

    # Model asset: copy the included toy config into benchmark_assets/model/
    cache_model_dir = CACHE_ROOT / "model"
    cache_model_dir.mkdir(parents=True, exist_ok=True)
    cached_cfg = cache_model_dir / "mnist.yaml"
    shutil.copy2(SRC_MODEL_CONFIG, cached_cfg)
    cfg_sha = sha256_file(cached_cfg)

    final_model_dir = MODEL_ROOT / "mnist_toy"
    final_model_dir.mkdir(parents=True, exist_ok=True)
    final_cfg = final_model_dir / "mnist.yaml"
    shutil.copy2(cached_cfg, final_cfg)

    assets["model"] = {
        "path": str(final_model_dir),
        "source": f"repo:{SRC_MODEL_CONFIG.relative_to(REPO_ROOT)}",
        "version": "repo",
        "sha256": cfg_sha,
    }

    write_extra(
        {
            "assets": assets,
            "meta": {
                "dataset_manifest": cache_manifest,
                "download_warnings": download_errors,
                "dataset_cache_root": str(cache_mnist_root),
                "model_config_source": str(SRC_MODEL_CONFIG),
                "model_config_path": str(final_cfg),
            },
        }
    )
except SystemExit:
    raise
except Exception as e:
    write_extra({"assets": assets, "failure_category": "unknown", "meta": {"prepare_error": f"{type(e).__name__}: {e}"}})
    raise
PY
BASH
)"

"$py_runner" "${repo_root}/benchmark_scripts/runner.py" "${runner_args[@]}" -- bash -lc "$cmd_str"
