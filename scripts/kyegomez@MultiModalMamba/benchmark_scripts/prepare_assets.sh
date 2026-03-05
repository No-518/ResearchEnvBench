#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model/weights) into benchmark_assets/.

Outputs (always written, even on failure):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Optional:
  --repo <path>            Repository root (default: auto-detect)
  --report-path <path>     Agent report path (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  --python <path>          Explicit python executable (highest priority)
  --offline                Do not attempt network downloads (use cache if available)

Notes:
  - Downloads go to benchmark_assets/cache/ first, then copied into:
      benchmark_assets/dataset/
      benchmark_assets/model/
EOF
}

repo_root=""
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_bin=""
offline=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo_root="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --offline) offline=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$repo_root" ]]; then
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

cd "$repo_root"

out_dir="$repo_root/build_output/prepare"
mkdir -p "$out_dir"
log_file="$out_dir/log.txt"
: > "$log_file"
exec > >(tee -a "$log_file") 2>&1

cache_dir="$repo_root/benchmark_assets/cache"
dataset_dir="$repo_root/benchmark_assets/dataset"
model_root="$repo_root/benchmark_assets/model"
mkdir -p "$cache_dir" "$dataset_dir" "$model_root"

json_py="$(command -v python3 || command -v python || true)"
if [[ -z "$json_py" ]]; then
  echo "python not found on PATH; cannot prepare assets." >&2
  exit 1
fi

resolved_python=""
python_resolution="unknown"

if [[ -n "$python_bin" ]]; then
  resolved_python="$python_bin"
  python_resolution="cli"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  resolved_python="${SCIMLOPSBENCH_PYTHON}"
  python_resolution="env"
else
  if [[ ! -f "$report_path" ]]; then
    echo "Missing report.json at: $report_path" >&2
    resolved_python=""
  else
    resolved_python="$("$json_py" - <<PY || true
import json, sys
from pathlib import Path
p = Path(r"""$report_path""")
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  sys.exit(2)
py = data.get("python_path")
if isinstance(py, str) and py.strip():
  print(py.strip())
PY
)"
    python_resolution="report"
  fi
fi

stage_py="$resolved_python"
if [[ -n "$stage_py" ]]; then
  if ! "$stage_py" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
    echo "Resolved python is not runnable: $stage_py" >&2
    stage_py=""
    resolved_python=""
  fi
fi
if [[ -z "$stage_py" ]]; then
  stage_py="$json_py"
fi

dataset_url="https://raw.githubusercontent.com/opencv/opencv/master/samples/data/lena.jpg"
model_id="hf-internal-testing/tiny-random-vit"
model_files=("config.json" "pytorch_model.bin")
model_base_url="https://huggingface.co/${model_id}/resolve/main"

OFFLINE_FLAG="$offline"
if [[ "${SCIMLOPSBENCH_OFFLINE:-0}" == "1" ]]; then
  OFFLINE_FLAG="1"
fi

export SCIMLOPSBENCH_REPORT="$report_path"
export SCIMLOPSBENCH_PYTHON="${SCIMLOPSBENCH_PYTHON:-}"

PYTHON_EXE="$resolved_python" PYTHON_RESOLUTION="$python_resolution" REPORT_PATH="$report_path" \
CACHE_DIR="$cache_dir" DATASET_DIR="$dataset_dir" MODEL_ROOT="$model_root" OUT_DIR="$out_dir" LOG_FILE="$log_file" \
DATASET_URL="$dataset_url" MODEL_ID="$model_id" MODEL_BASE_URL="$model_base_url" MODEL_FILES="${model_files[*]}" \
OFFLINE="$OFFLINE_FLAG" \
  "$stage_py" - <<'PY'
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Dict, Tuple

cache_dir = Path(os.environ["CACHE_DIR"])
dataset_dir = Path(os.environ["DATASET_DIR"])
model_root = Path(os.environ["MODEL_ROOT"])
out_dir = Path(os.environ["OUT_DIR"])
log_file = Path(os.environ["LOG_FILE"])

dataset_url = os.environ["DATASET_URL"]
model_id = os.environ["MODEL_ID"]
model_base_url = os.environ["MODEL_BASE_URL"].rstrip("/")
model_files = [p for p in os.environ.get("MODEL_FILES", "").split() if p]

offline = os.environ.get("OFFLINE", "0") == "1"

report_path = os.environ.get("REPORT_PATH", "")
python_exe = os.environ.get("PYTHON_EXE", "").strip()
python_resolution = os.environ.get("PYTHON_RESOLUTION", "unknown")

def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def git_commit(repo_root: Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True).strip()
    except Exception:
        return ""

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def download(url: str, dest: Path, timeout: int = 60) -> Dict[str, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    headers: Dict[str, str] = {}
    req = urllib.request.Request(url, headers={"User-Agent": "scimlopsbench/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        with tmp.open("wb") as f:
            shutil.copyfileobj(resp, f)
    tmp.replace(dest)
    return headers

def tail_log(max_lines: int = 220) -> str:
    try:
        txt = log_file.read_text(encoding="utf-8", errors="replace")
        lines = txt.splitlines()[-max_lines:]
        return "\n".join(lines)
    except Exception:
        return ""

def empty_assets() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": str(dataset_dir), "source": dataset_url, "version": "", "sha256": ""},
        "model": {"path": str(model_root), "source": model_base_url, "version": "", "sha256": ""},
    }

manifest_path = cache_dir / "manifest.json"
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
except Exception:
    manifest = {}

dataset_cache = cache_dir / "dataset" / "lena.jpg"
dataset_final = dataset_dir / "lena.jpg"
model_dir = model_root / "tiny-random-vit"
model_cache_dir = cache_dir / "model" / "tiny-random-vit"

assets = empty_assets()
failure_category = "unknown"
status = "failure"
exit_code = 1
error_excerpt = ""

def prepare_one_file(url: str, cache_path: Path, final_path: Path, key: str) -> Tuple[str, str]:
    existing_sha = ""
    recorded_sha = str(manifest.get(key, {}).get("sha256", "")).strip()
    recorded_ver = str(manifest.get(key, {}).get("version", "")).strip()

    if cache_path.exists():
        try:
            existing_sha = sha256_file(cache_path)
        except Exception:
            existing_sha = ""

    if existing_sha and recorded_sha and existing_sha == recorded_sha:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache_path, final_path)
        return recorded_sha, recorded_ver or f"sha256:{recorded_sha}"

    if offline:
        if existing_sha:
            ver = recorded_ver or f"sha256:{existing_sha}"
            manifest[key] = {"sha256": existing_sha, "version": ver, "source": url}
            final_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cache_path, final_path)
            return existing_sha, ver
        raise RuntimeError(f"offline and cache missing for {key}: {cache_path}")

    headers = download(url, cache_path)
    new_sha = sha256_file(cache_path)
    ver = ""
    if headers.get("etag"):
        ver = f"etag:{headers['etag']}"
    elif headers.get("last-modified"):
        ver = f"last_modified:{headers['last-modified']}"
    else:
        ver = f"sha256:{new_sha}"

    manifest[key] = {"sha256": new_sha, "version": ver, "source": url}
    final_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cache_path, final_path)
    return new_sha, ver

try:
    if not python_exe:
        raise RuntimeError(f"missing_report: cannot resolve python (report_path={report_path})")

    # Dataset: single public image.
    ds_sha, ds_ver = prepare_one_file(dataset_url, dataset_cache, dataset_final, "dataset_lena_jpg")
    assets["dataset"] = {
        "path": str(dataset_dir),
        "source": dataset_url,
        "version": ds_ver,
        "sha256": ds_sha,
    }

    # Model: minimal public weights/config from Hugging Face (no auth).
    model_dir.mkdir(parents=True, exist_ok=True)
    model_cache_dir.mkdir(parents=True, exist_ok=True)

    model_sha_main = ""
    model_ver_main = ""
    for fname in model_files:
        url = f"{model_base_url}/{fname}"
        cache_path = model_cache_dir / fname
        final_path = model_dir / fname
        sha, ver = prepare_one_file(url, cache_path, final_path, f"model_{model_id}_{fname}".replace("/", "__"))
        if fname == "pytorch_model.bin":
            model_sha_main = sha
            model_ver_main = ver

    # Robust "resolved model directory": verify existence and expected artifacts.
    expected = [model_dir / "config.json", model_dir / "pytorch_model.bin"]
    missing = [p.name for p in expected if not p.exists()]
    if missing:
        raise RuntimeError(
            f"model artifacts missing after download/copy; search_root={model_root} missing={missing}"
        )

    assets["model"] = {
        "path": str(model_dir),
        "source": model_base_url,
        "version": model_ver_main or (f"sha256:{model_sha_main}" if model_sha_main else "unknown"),
        "sha256": model_sha_main,
    }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    status = "success"
    exit_code = 0
    failure_category = "unknown"
except Exception as e:
    msg = str(e)
    if "missing_report" in msg:
        failure_category = "missing_report"
    elif "offline" in msg or "urlopen error" in msg or "temporary failure" in msg.lower():
        failure_category = "download_failed"
    elif "model artifacts missing" in msg:
        failure_category = "model"
    else:
        failure_category = "download_failed"
    status = "failure"
    exit_code = 1
    tb = traceback.format_exc()
    error_excerpt = "\n".join(tb.splitlines()[-220:])

results = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": "prepare",
    "task": "download",
    "command": f"bash benchmark_scripts/prepare_assets.sh --repo {Path('.').resolve()}",
    "timeout_sec": 1200,
    "framework": "unknown",
    "assets": assets,
    "meta": {
        "python": python_exe,
        "git_commit": git_commit(Path('.').resolve()),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            "SCIMLOPSBENCH_OFFLINE": os.environ.get("SCIMLOPSBENCH_OFFLINE", ""),
        },
        "decision_reason": (
            "Repo docs do not specify datasets/checkpoints; downloaded tiny public assets "
            "(1 image + tiny HF ViT weights) to satisfy reproducible prepare stage."
        ),
        "python_resolution": python_resolution,
        "report_path": report_path,
        "timestamp_utc": utcnow(),
    },
    "failure_category": failure_category,
    "error_excerpt": error_excerpt or (tail_log() if status == "failure" else ""),
}

(out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

sys.exit(exit_code)
PY
