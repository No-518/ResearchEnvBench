#!/usr/bin/env bash
set -u
set -o pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets: download minimal dataset + minimal model/weights.

Outputs (always, even on failure):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Assets:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Optional:
  --repo <path>            Repo root (default: cwd)
  --python <path>          Override python interpreter
  --report-path <path>     Agent report path (default: /opt/scimlopsbench/report.json)
EOF
}

repo="."
python_bin=""
report_path=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

# Prevent creating __pycache__ in the repo or environment.
export PYTHONDONTWRITEBYTECODE=1

repo="$(python -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$repo" 2>/dev/null || echo "$repo")"
stage_dir="$repo/build_output/prepare"
mkdir -p "$stage_dir"

log_path="$stage_dir/log.txt"
results_json="$stage_dir/results.json"
: >"$log_path"
exec > >(tee -a "$log_path") 2>&1

echo "stage=prepare"
echo "repo=$repo"
echo "out_dir=$stage_dir"
echo "timestamp_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cd "$repo" || {
  echo "Failed to cd to repo: $repo" >&2
  cat >"$results_json" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
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
}
JSON
  exit 1
}

# Resolve python interpreter (must come from report unless overridden).
py_cmd=()
if [[ -n "$python_bin" ]]; then
  py_cmd=("$python_bin")
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  py_cmd=("${SCIMLOPSBENCH_PYTHON}")
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
    if [[ -n "$py_from_report" ]]; then
      py_cmd=("$py_from_report")
    else
      echo "Report exists but python_path missing/invalid; refusing to guess python." >&2
      cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "report.json missing python_path: $rp"},
  "failure_category": "missing_report",
  "error_excerpt": ""
}
JSON
      exit 1
    fi
  else
    echo "Missing report.json and no --python/SCIMLOPSBENCH_PYTHON provided: $rp" >&2
    cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "missing report.json: $rp"},
  "failure_category": "missing_report",
  "error_excerpt": ""
}
JSON
    exit 1
  fi
fi

echo "python_cmd=${py_cmd[*]}"
if ! "${py_cmd[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "Failed to run python via: ${py_cmd[*]}" >&2
  cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "${py_cmd[*]}",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {"python": "${py_cmd[*]}", "git_commit": "", "env_vars": {}, "decision_reason": "python not runnable"},
  "failure_category": "missing_report",
  "error_excerpt": ""
}
JSON
  exit 1
fi

# Constrain caches to benchmark_assets/cache/
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

export BENCHMARK_ASSETS_DIR="$repo/benchmark_assets"
export PREPARE_STAGE_DIR="$stage_dir"
export PREPARE_RESULTS_JSON="$results_json"

timeout 1200s "${py_cmd[@]}" - <<'PY' || exit 1
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(os.getcwd()).resolve()
ASSETS_DIR = Path(os.environ["BENCHMARK_ASSETS_DIR"]).resolve()
STAGE_DIR = Path(os.environ["PREPARE_STAGE_DIR"]).resolve()
RESULTS_PATH = Path(os.environ["PREPARE_RESULTS_JSON"]).resolve()
LOG_PATH = STAGE_DIR / "log.txt"


def write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tail(path: Path, max_lines: int = 240) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        return "".join(lines[-max_lines:])
    except Exception:
        return ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_symlink_or_copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=True)
        return
    except Exception:
        pass
    shutil.copytree(src, dst)


def download_url(url: str, dst: Path, *, timeout_sec: int = 60) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "scimlopsbench/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        with tmp.open("wb") as f:
            shutil.copyfileobj(resp, f)
        tmp.replace(dst)


def ensure_dataset() -> dict:
    # Minimal "dataset" = one image.
    # Source chosen for anonymous public access and stability.
    dataset_url = "https://raw.githubusercontent.com/python-pillow/Pillow/master/Tests/images/hopper.jpg"
    dataset_version = "pillow/Pillow@master (file: Tests/images/hopper.jpg)"

    cache_path = ASSETS_DIR / "cache" / "dataset" / "hopper.jpg"
    final_path = ASSETS_DIR / "dataset" / "hopper.jpg"

    # Reuse cache if present.
    if cache_path.exists():
        if not final_path.exists():
            final_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cache_path, final_path)
        digest = sha256_file(final_path)
        return {"path": str(final_path), "source": dataset_url, "version": dataset_version, "sha256": digest}

    # Download into cache first.
    try:
        download_url(dataset_url, cache_path)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache_path, final_path)
        digest = sha256_file(final_path)
        return {"path": str(final_path), "source": dataset_url, "version": dataset_version, "sha256": digest}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Dataset download HTTPError: {e}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Dataset download URLError (offline?): {e}") from e


def ensure_model() -> dict:
    # Minimal model used in code: modules/detection/rtdetr_v2_onnx.py
    repo_id = "ogkalu/comic-text-and-bubble-detector"
    allow = ["detector.onnx", "config.json"]

    cache_dir = ASSETS_DIR / "cache" / "hf_models" / "ogkalu__comic-text-and-bubble-detector"
    final_dir = ASSETS_DIR / "model" / "comic-text-and-bubble-detector"

    try:
        from huggingface_hub import snapshot_download
    except Exception as e:
        raise RuntimeError(f"huggingface_hub not importable: {e}") from e

    # Attempt online download first; if that fails, try offline reuse.
    resolved_dir: Path | None = None
    online_error: str = ""
    try:
        resolved = snapshot_download(
            repo_id=repo_id,
            local_dir=str(cache_dir),
            local_dir_use_symlinks=False,
            allow_patterns=allow,
        )
        resolved_dir = Path(resolved)
    except Exception as e:
        online_error = f"{type(e).__name__}: {e}"
        try:
            resolved = snapshot_download(
                repo_id=repo_id,
                local_dir=str(cache_dir),
                local_dir_use_symlinks=False,
                allow_patterns=allow,
                local_files_only=True,
            )
            resolved_dir = Path(resolved)
        except Exception as e2:
            raise RuntimeError(
                "Model download failed and no offline cache available.\n"
                f"Online error: {online_error}\n"
                f"Offline error: {type(e2).__name__}: {e2}\n"
                f"Cache dir: {cache_dir}"
            ) from e2

    assert resolved_dir is not None
    # Verify expected artifacts exist.
    detector = resolved_dir / "detector.onnx"
    cfg = resolved_dir / "config.json"
    if not detector.exists():
        raise RuntimeError(
            "Downloader reported success but expected detector.onnx not found.\n"
            f"Resolved dir: {resolved_dir}\n"
            f"Search root: {cache_dir}"
        )
    if not cfg.exists():
        raise RuntimeError(
            "Downloader reported success but expected config.json not found.\n"
            f"Resolved dir: {resolved_dir}\n"
            f"Search root: {cache_dir}"
        )

    safe_symlink_or_copytree(resolved_dir, final_dir)

    digest = sha256_file(detector)
    return {
        "path": str(final_dir),
        "source": f"https://huggingface.co/{repo_id}",
        "version": "unknown",
        "sha256": digest,
    }


def main() -> int:
    stage_result = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "prepare",
        "task": "download",
        "command": "prepare_assets.sh",
        "timeout_sec": 1200,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": sys.executable,
            "git_commit": "",
            "env_vars": {
                k: os.environ.get(k, "")
                for k in [
                    "HF_HOME",
                    "HUGGINGFACE_HUB_CACHE",
                    "HF_HUB_CACHE",
                    "XDG_CACHE_HOME",
                    "XDG_CONFIG_HOME",
                ]
                if k in os.environ
            },
            "decision_reason": (
                "Model chosen from README + modules/detection/rtdetr_v2_onnx.py (HF repo ogkalu/comic-text-and-bubble-detector). "
                "Dataset chosen as a single public image for minimal offline-cacheable inference input."
            ),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    try:
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        (ASSETS_DIR / "cache").mkdir(parents=True, exist_ok=True)
        (ASSETS_DIR / "dataset").mkdir(parents=True, exist_ok=True)
        (ASSETS_DIR / "model").mkdir(parents=True, exist_ok=True)

        dataset = ensure_dataset()
        model = ensure_model()
        stage_result["assets"]["dataset"] = dataset
        stage_result["assets"]["model"] = model
        stage_result["status"] = "success"
        stage_result["exit_code"] = 0
        stage_result["failure_category"] = "unknown"
        stage_result["error_excerpt"] = ""
        write_json(RESULTS_PATH, stage_result)
        return 0
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        with LOG_PATH.open("a", encoding="utf-8", errors="replace") as f:
            f.write("\nERROR:\n")
            f.write(msg + "\n")
            f.write(traceback.format_exc() + "\n")

        stage_result["status"] = "failure"
        stage_result["exit_code"] = 1
        # Categorize best-effort.
        low = msg.lower()
        if "huggingface_hub not importable" in low:
            stage_result["failure_category"] = "deps"
        elif "offline" in low or "urlerror" in low or "connection" in low:
            stage_result["failure_category"] = "download_failed"
        elif "expected detector.onnx not found" in low:
            stage_result["failure_category"] = "model"
        elif "dataset download" in low:
            stage_result["failure_category"] = "data"
        elif "401" in low or "403" in low or "token" in low:
            stage_result["failure_category"] = "auth_required"
        else:
            stage_result["failure_category"] = "unknown"
        stage_result["error_excerpt"] = tail(LOG_PATH)
        write_json(RESULTS_PATH, stage_result)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
PY
