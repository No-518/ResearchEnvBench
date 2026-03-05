#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset download + minimal model download).

Writes:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Assets layout:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Options:
  --python <path>        Use explicit python (overrides report/env)
  --report-path <path>   Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
EOF
}

python_bin=""
report_path=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
report_path="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"

stage_dir="$repo_root/build_output/prepare"
mkdir -p "$stage_dir"
log="$stage_dir/log.txt"
results_json="$stage_dir/results.json"

command_str="bash benchmark_scripts/prepare_assets.sh"
[[ -n "$python_bin" ]] && command_str+=" --python $python_bin"
[[ -n "${SCIMLOPSBENCH_REPORT:-}" ]] && command_str+=" (SCIMLOPSBENCH_REPORT=$SCIMLOPSBENCH_REPORT)"

# Keep all generated caches under benchmark_assets/cache and prevent __pycache__ in repo.
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export BENCHMARK_ASSETS_DIR="$repo_root/benchmark_assets"
export XDG_CACHE_HOME="$BENCHMARK_ASSETS_DIR/cache/xdg"
export PIP_CACHE_DIR="$BENCHMARK_ASSETS_DIR/cache/pip"
export HF_HOME="$BENCHMARK_ASSETS_DIR/cache/huggingface"
export TRANSFORMERS_CACHE="$BENCHMARK_ASSETS_DIR/cache/huggingface/transformers"
export HF_DATASETS_CACHE="$BENCHMARK_ASSETS_DIR/cache/huggingface/datasets"
export TORCH_HOME="$BENCHMARK_ASSETS_DIR/cache/torch"
export SENTENCE_TRANSFORMERS_HOME="$BENCHMARK_ASSETS_DIR/cache/sentence_transformers"
export TMPDIR="$BENCHMARK_ASSETS_DIR/cache/tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
mkdir -p "$XDG_CACHE_HOME" "$PIP_CACHE_DIR" "$HF_HOME" "$TRANSFORMERS_CACHE" "$HF_DATASETS_CACHE" "$TORCH_HOME" "$SENTENCE_TRANSFORMERS_HOME" "$TMPDIR"
mkdir -p "$BENCHMARK_ASSETS_DIR/cache" "$BENCHMARK_ASSETS_DIR/dataset" "$BENCHMARK_ASSETS_DIR/model"

status="failure"
skip_reason="not_applicable"
exit_code=1
failure_category="unknown"
decision_reason="Use RelBench Rel-F1 dataset because repo entrypoints with configurable cache_dir are examples/rdl.py and examples/relgnn.py; minimal model is sentence-transformers/average_word_embeddings_glove.6B.300d used during Rel-F1 preprocessing."

py_install_attempted=0
py_install_command=""
model_repo_id="sentence-transformers/average_word_embeddings_glove.6B.300d"
model_revision="main"
model_cache_dir="$BENCHMARK_ASSETS_DIR/cache/model"
model_final_dir="$BENCHMARK_ASSETS_DIR/model"
model_resolved_dir=""

dataset_name="rel-f1"
dataset_base_url="https://relbench.stanford.edu/download/rel-f1/"
dataset_cache_dir="$BENCHMARK_ASSETS_DIR/cache/dataset/$dataset_name"
dataset_dir="$BENCHMARK_ASSETS_DIR/dataset"

dataset_sha256=""
model_sha256=""
prev_dataset_sha256=""
prev_model_sha256=""
dataset_reuse_verified=0
model_reuse_verified=0
force_redownload_dataset=0
force_redownload_model=0

mkdir -p "$dataset_cache_dir" "$model_cache_dir"

echo "[prepare] repo_root=$repo_root" >"$log"
echo "[prepare] report_path=$report_path" >>"$log"
echo "[prepare] decision_reason=$decision_reason" >>"$log"

resolve_report_python() {
  REPORT_PATH="$report_path" python - <<'PY'
import json
import os
import pathlib
import sys

rp = pathlib.Path(os.environ["REPORT_PATH"])
try:
    data = json.loads(rp.read_text(encoding="utf-8"))
except Exception as e:
    print(f"ERROR: invalid report json: {e}", file=sys.stderr)
    sys.exit(1)
py = data.get("python_path")
if not isinstance(py, str) or not py.strip():
    print("ERROR: report missing python_path", file=sys.stderr)
    sys.exit(1)
print(py)
PY
}

if [[ -z "$python_bin" ]]; then
  python_bin="$(resolve_report_python)" || {
    failure_category="missing_report"
    echo "[prepare] ERROR: cannot resolve python from report" >>"$log"
    RESULTS_PATH="$results_json" LOG_PATH="$log" COMMAND_STR="$command_str" DECISION_REASON="$decision_reason" python - <<'PY'
import json
import os
import pathlib

p = pathlib.Path(os.environ["RESULTS_PATH"])
logp = pathlib.Path(os.environ["LOG_PATH"])
command_str = os.environ.get("COMMAND_STR", "")
decision_reason = os.environ.get("DECISION_REASON", "")

tail = "\n".join(logp.read_text(encoding="utf-8", errors="replace").splitlines()[-220:])
p.write_text(
    json.dumps(
        {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "prepare",
            "task": "download",
            "command": command_str,
            "timeout_sec": 1200,
            "framework": "pytorch",
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "meta": {
                "python": "",
                "git_commit": "",
                "env_vars": {},
                "decision_reason": decision_reason,
            },
            "failure_category": "missing_report",
            "error_excerpt": tail,
        },
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
PY
    exit 1
  }
fi

echo "[prepare] using python_bin=$python_bin" >>"$log"

if [[ -f "$results_json" ]]; then
  read -r prev_status prev_dataset_sha256 prev_model_sha256 < <(
    PREV_RESULTS_PATH="$results_json" "$python_bin" - <<'PY' 2>/dev/null || true
import json, os, pathlib
p = pathlib.Path(os.environ["PREV_RESULTS_PATH"])
try:
    d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print("", "", "")
    raise SystemExit(0)
status = d.get("status", "")
assets = d.get("assets", {}) if isinstance(d.get("assets"), dict) else {}
ds = assets.get("dataset", {}) if isinstance(assets.get("dataset"), dict) else {}
md = assets.get("model", {}) if isinstance(assets.get("model"), dict) else {}
print(status, ds.get("sha256", "") or "", md.get("sha256", "") or "")
PY
  )
  if [[ "${prev_status:-}" != "success" ]]; then
    prev_dataset_sha256=""
    prev_model_sha256=""
  fi
  if [[ -n "$prev_dataset_sha256" || -n "$prev_model_sha256" ]]; then
    echo "[prepare] Found previous prepare results: dataset_sha256=${prev_dataset_sha256:-} model_sha256=${prev_model_sha256:-}" >>"$log"
  fi
fi

finalize() {
  local rc="$1"
  if [[ "$rc" -eq 0 ]]; then
    status="success"
    exit_code=0
    failure_category="unknown"
  else
    status="${status:-failure}"
    exit_code=1
    failure_category="${failure_category:-unknown}"
  fi
  STAGE_STATUS="$status" \
  STAGE_SKIP_REASON="$skip_reason" \
  STAGE_EXIT_CODE="$exit_code" \
  STAGE_FAILURE_CATEGORY="$failure_category" \
  COMMAND_STR="$command_str" \
  PYTHON_BIN="$python_bin" \
  DECISION_REASON="$decision_reason" \
  INSTALL_ATTEMPTED="$py_install_attempted" \
  INSTALL_COMMAND="$py_install_command" \
  DATASET_DIR="$dataset_dir" \
  DATASET_SOURCE="$dataset_base_url" \
  DATASET_VERSION="$dataset_name" \
  DATASET_SHA256="$dataset_sha256" \
  PREV_DATASET_SHA256="$prev_dataset_sha256" \
  DATASET_REUSE_VERIFIED="$dataset_reuse_verified" \
  MODEL_DIR="$model_resolved_dir" \
  MODEL_SOURCE="$model_repo_id" \
  MODEL_VERSION="$model_revision" \
  MODEL_SHA256="$model_sha256" \
  PREV_MODEL_SHA256="$prev_model_sha256" \
  MODEL_REUSE_VERIFIED="$model_reuse_verified" \
  REPO_ROOT="$repo_root" \
  LOG_PATH="$log" \
  RESULTS_PATH="$results_json" \
  python - <<'PY'
import json
import os
import pathlib
import subprocess
import time

repo_root = pathlib.Path(os.environ["REPO_ROOT"])
log_path = pathlib.Path(os.environ["LOG_PATH"])
results_path = pathlib.Path(os.environ["RESULTS_PATH"])

def tail(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""

git_commit = ""
try:
    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()
except Exception:
    git_commit = ""

assets = {
    "dataset": {
        "path": os.environ.get("DATASET_DIR", ""),
        "source": os.environ.get("DATASET_SOURCE", ""),
        "version": os.environ.get("DATASET_VERSION", ""),
        "sha256": os.environ.get("DATASET_SHA256", ""),
    },
    "model": {
        "path": os.environ.get("MODEL_DIR", ""),
        "source": os.environ.get("MODEL_SOURCE", ""),
        "version": os.environ.get("MODEL_VERSION", ""),
        "sha256": os.environ.get("MODEL_SHA256", ""),
    },
}

results = {
    "status": os.environ.get("STAGE_STATUS", "failure"),
    "skip_reason": os.environ.get("STAGE_SKIP_REASON", "not_applicable"),
    "exit_code": int(os.environ.get("STAGE_EXIT_CODE", "1")),
    "stage": "prepare",
    "task": "download",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 1200,
    "framework": "pytorch",
    "assets": assets,
    "meta": {
        "python": os.environ.get("PYTHON_BIN", ""),
        "git_commit": git_commit,
        "env_vars": {
            k: os.environ.get(k, "")
            for k in [
                "HF_HOME",
                "TRANSFORMERS_CACHE",
                "HF_DATASETS_CACHE",
                "PIP_CACHE_DIR",
                "XDG_CACHE_HOME",
                "SENTENCE_TRANSFORMERS_HOME",
                "TORCH_HOME",
                "TMPDIR",
                "PYTHONDONTWRITEBYTECODE",
                "SCIMLOPSBENCH_REPORT",
                "SCIMLOPSBENCH_PYTHON",
            ]
        },
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "install_attempted": bool(int(os.environ.get("INSTALL_ATTEMPTED", "0"))),
        "install_command": os.environ.get("INSTALL_COMMAND", ""),
        "reuse_verified_by_sha256": {
            "dataset": bool(int(os.environ.get("DATASET_REUSE_VERIFIED", "0"))),
            "model": bool(int(os.environ.get("MODEL_REUSE_VERIFIED", "0"))),
        },
        "previous_sha256": {
            "dataset": os.environ.get("PREV_DATASET_SHA256", ""),
            "model": os.environ.get("PREV_MODEL_SHA256", ""),
        },
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    },
    "failure_category": os.environ.get("STAGE_FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail(log_path),
}

results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

trap 'rc=$?; finalize "$rc"; exit "$exit_code"' EXIT

echo "[prepare] Ensuring sentence-transformers is importable..." >>"$log"
if ! "$python_bin" -c "import sentence_transformers" >>"$log" 2>&1; then
  py_install_attempted=1
  py_install_command="$python_bin -m pip install -q sentence-transformers"
  echo "[prepare] Installing sentence-transformers via: $py_install_command" >>"$log"
  if ! "$python_bin" -m pip install -q sentence-transformers >>"$log" 2>&1; then
    if grep -Eqi "Temporary failure|Name or service not known|Connection.*failed|timed out|No route to host" "$log"; then
      failure_category="download_failed"
    else
      failure_category="deps"
    fi
    exit 1
  fi
fi

echo "[prepare] Downloading minimal model: $model_repo_id" >>"$log"
model_cache_local="$model_cache_dir/${model_repo_id//\//__}"
model_final_local="$model_final_dir/${model_repo_id//\//__}"

compute_dir_sha256() {
  local dir_path="$1"
  DIR_PATH="$dir_path" "$python_bin" - <<'PY'
import hashlib, os, pathlib
root = pathlib.Path(os.environ["DIR_PATH"])
h = hashlib.sha256()
files = [p for p in root.rglob("*") if p.is_file()]
for p in sorted(files, key=lambda x: str(x.relative_to(root))):
    rel = str(p.relative_to(root)).replace("\\\\", "/")
    try:
        data = p.read_bytes()
    except Exception:
        continue
    fh = hashlib.sha256(data).hexdigest()
    h.update((rel + "\0" + fh + "\n").encode("utf-8"))
print(h.hexdigest())
PY
}

if [[ -n "$prev_model_sha256" && -d "$model_final_local" ]]; then
  cur_model_sha="$(compute_dir_sha256 "$model_final_local" 2>>"$log" || true)"
  if [[ -n "$cur_model_sha" && "$cur_model_sha" == "$prev_model_sha256" ]]; then
    model_reuse_verified=1
    echo "[prepare] Model sha256 verified; skipping download." >>"$log"
  else
    force_redownload_model=1
    echo "[prepare] WARNING: model sha256 mismatch vs previous; will attempt to re-download." >>"$log"
  fi
fi

need_model_download=1
if [[ -d "$model_final_local" && "$force_redownload_model" -eq 0 ]]; then
  model_resolved_dir="$model_final_local"
  echo "[prepare] Model already present at $model_resolved_dir" >>"$log"
  need_model_download=0
fi

if [[ "$need_model_download" -eq 1 ]]; then
  model_resolved_dir="$model_final_local"
  if ! MODEL_REPO_ID="$model_repo_id" MODEL_REVISION="$model_revision" MODEL_CACHE_LOCAL="$model_cache_local" FORCE_DOWNLOAD="$force_redownload_model" "$python_bin" - <<'PY' >>"$log" 2>&1; then
import os, pathlib, sys

repo_id = os.environ["MODEL_REPO_ID"]
revision = os.environ.get("MODEL_REVISION", "main")
cache_dir = pathlib.Path(os.environ["HF_HOME"])  # already under benchmark_assets/cache
local_dir = pathlib.Path(os.environ["MODEL_CACHE_LOCAL"])
force_download = os.environ.get("FORCE_DOWNLOAD", "0") == "1"
local_dir.parent.mkdir(parents=True, exist_ok=True)

try:
    from huggingface_hub import snapshot_download
except Exception as e:
    print(f"ERROR: missing huggingface_hub: {e}", file=sys.stderr)
    sys.exit(2)

def try_download(local_files_only: bool):
    kwargs = dict(
        repo_id=repo_id,
        revision=revision,
        cache_dir=str(cache_dir),
        local_dir=str(local_dir),
        local_dir_use_symlinks=False,
        local_files_only=local_files_only,
    )
    if force_download:
        try:
            import inspect
            if "force_download" in inspect.signature(snapshot_download).parameters:
                kwargs["force_download"] = True
        except Exception:
            pass
    return snapshot_download(**kwargs)

try:
    p = try_download(local_files_only=False)
except Exception as e:
    print(f"WARNING: online snapshot_download failed: {e}", file=sys.stderr)
    p = try_download(local_files_only=True)

print(p)
PY
    # If download failed, allow offline reuse only if cache dir exists.
    if [[ -d "$model_cache_local" ]]; then
      echo "[prepare] WARNING: model download failed, using existing cache at $model_cache_local" >>"$log"
    else
      if grep -Eqi "Connection|timed out|No route|Temporary failure" "$log"; then
        failure_category="download_failed"
      else
        failure_category="deps"
      fi
      exit 1
    fi
  fi

  # Link/copy from cache -> benchmark_assets/model
  if [[ -d "$model_cache_local" && ! -e "$model_final_local" ]]; then
    ln -s "$model_cache_local" "$model_final_local" 2>>"$log" || cp -a "$model_cache_local" "$model_final_local"
  fi
  if [[ ! -d "$model_final_local" ]]; then
    failure_category="model"
    echo "[prepare] ERROR: model resolved dir not found after download: $model_final_local (cache root: $model_cache_dir)" >>"$log"
    exit 1
  fi
  model_resolved_dir="$model_final_local"
fi

echo "[prepare] Downloading dataset zips to cache: $dataset_base_url" >>"$log"
mkdir -p "$dataset_cache_dir"

db_zip="$dataset_cache_dir/db.zip"
dnf_zip="$dataset_cache_dir/tasks_driver-dnf.zip"
pos_zip="$dataset_cache_dir/tasks_driver-position.zip"
top3_zip="$dataset_cache_dir/tasks_driver-top3.zip"

compute_dataset_bundle_sha256() {
  DB_ZIP="$db_zip" DNF_ZIP="$dnf_zip" POS_ZIP="$pos_zip" TOP3_ZIP="$top3_zip" "$python_bin" - <<'PY'
import hashlib, os, pathlib
paths = [
    pathlib.Path(os.environ["DB_ZIP"]),
    pathlib.Path(os.environ["DNF_ZIP"]),
    pathlib.Path(os.environ["POS_ZIP"]),
    pathlib.Path(os.environ["TOP3_ZIP"]),
]
h = hashlib.sha256()
for p in paths:
    b = p.read_bytes()
    fh = hashlib.sha256(b).hexdigest()
    h.update((p.name + "\0" + fh + "\n").encode("utf-8"))
print(h.hexdigest())
PY
}

if [[ -n "$prev_dataset_sha256" && -f "$db_zip" && -f "$dnf_zip" && -f "$pos_zip" && -f "$top3_zip" ]]; then
  cur_dataset_sha="$(compute_dataset_bundle_sha256 2>>"$log" || true)"
  if [[ -n "$cur_dataset_sha" && "$cur_dataset_sha" == "$prev_dataset_sha256" ]]; then
    dataset_reuse_verified=1
    echo "[prepare] Dataset zip bundle sha256 verified; skipping downloads." >>"$log"
  else
    force_redownload_dataset=1
    echo "[prepare] WARNING: dataset zip bundle sha256 mismatch vs previous; will attempt to re-download." >>"$log"
  fi
fi

download_zip() {
  local url="$1"
  local dest="$2"
  local force="${3:-0}"
  if [[ "$force" == "0" && -f "$dest" ]]; then
    echo "[prepare] cache hit: $dest" >>"$log"
    return 0
  fi
  echo "[prepare] downloading: $url -> $dest" >>"$log"
  local tmp="${dest}.tmp.$$"
  if ! DOWNLOAD_URL="$url" DOWNLOAD_DEST="$tmp" "$python_bin" - <<'PY' >>"$log" 2>&1; then
import os
import pathlib
import sys
import urllib.request
url = os.environ["DOWNLOAD_URL"]
dest = pathlib.Path(os.environ["DOWNLOAD_DEST"])
dest.parent.mkdir(parents=True, exist_ok=True)
try:
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    dest.write_bytes(data)
except Exception as e:
    print(f"ERROR: download failed: {e}", file=sys.stderr)
    sys.exit(1)
PY
    rm -f "$tmp" 2>/dev/null || true
    return 1
  fi
  mv -f "$tmp" "$dest"
}

download_zip "${dataset_base_url}db.zip" "$db_zip" "$force_redownload_dataset" || {
  [[ -f "$db_zip" ]] || { failure_category="download_failed"; exit 1; }
  echo "[prepare] WARNING: using existing db.zip from cache" >>"$log"
}
download_zip "${dataset_base_url}tasks/driver-dnf.zip" "$dnf_zip" "$force_redownload_dataset" || {
  [[ -f "$dnf_zip" ]] || { failure_category="download_failed"; exit 1; }
  echo "[prepare] WARNING: using existing driver-dnf.zip from cache" >>"$log"
}
download_zip "${dataset_base_url}tasks/driver-position.zip" "$pos_zip" "$force_redownload_dataset" || {
  [[ -f "$pos_zip" ]] || { failure_category="download_failed"; exit 1; }
  echo "[prepare] WARNING: using existing driver-position.zip from cache" >>"$log"
}
download_zip "${dataset_base_url}tasks/driver-top3.zip" "$top3_zip" "$force_redownload_dataset" || {
  [[ -f "$top3_zip" ]] || { failure_category="download_failed"; exit 1; }
  echo "[prepare] WARNING: using existing driver-top3.zip from cache" >>"$log"
}

echo "[prepare] Extracting dataset into benchmark_assets/dataset/rel-f1/raw/ ..." >>"$log"
dataset_root="$BENCHMARK_ASSETS_DIR/dataset/$dataset_name"
raw_dir="$dataset_root/raw"
tasks_dir="$raw_dir/tasks"
mkdir -p "$raw_dir" "$tasks_dir"

extract_zip() {
  local zip_path="$1"
  local dest_dir="$2"
  ZIP_PATH="$zip_path" ZIP_DEST_DIR="$dest_dir" "$python_bin" - <<'PY' >>"$log" 2>&1
import os
import pathlib
import zipfile
zip_path = pathlib.Path(os.environ["ZIP_PATH"])
dest = pathlib.Path(os.environ["ZIP_DEST_DIR"])
dest.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(zip_path, "r") as z:
    z.extractall(dest)
PY
}

# db.zip contains db/...
extract_zip "$db_zip" "$raw_dir"
extract_zip "$dnf_zip" "$tasks_dir"
extract_zip "$pos_zip" "$tasks_dir"
extract_zip "$top3_zip" "$tasks_dir"

echo "[prepare] Processing dataset via rllm.datasets.RelF1Dataset with CUDA disabled (CPU-safe caches)..." >>"$log"
if ! CUDA_VISIBLE_DEVICES="" REPO_ROOT="$repo_root" DATASET_DIR="$dataset_dir" DATASET_NAME="$dataset_name" "$python_bin" - <<'PY' >>"$log" 2>&1; then
import os
import pathlib
import shutil
import sys
import traceback

repo_root = pathlib.Path(os.environ["REPO_ROOT"])
os.chdir(repo_root)

from rllm.datasets import RelF1Dataset

cached_dir = pathlib.Path(os.environ["DATASET_DIR"])
dataset_name = os.environ.get("DATASET_NAME", "rel-f1").strip() or "rel-f1"
processed_dir = cached_dir / dataset_name / "processed"

def load_all(force_reload: bool) -> None:
    ds = RelF1Dataset(cached_dir=str(cached_dir), force_reload=force_reload)
    ds.load_all()

try:
    load_all(force_reload=False)
    print("RelF1Dataset load_all OK (CPU-safe).")
except Exception as e:
    msg = str(e)
    print(f"WARNING: RelF1Dataset load_all failed with CUDA disabled: {msg}", file=sys.stderr)
    if "Attempting to deserialize object on a CUDA device" in msg or "validate_cuda_device" in msg:
        print(f"Reprocessing on CPU: removing {processed_dir}", file=sys.stderr)
        shutil.rmtree(processed_dir, ignore_errors=True)
        load_all(force_reload=True)
        print("RelF1Dataset reprocessed on CPU and load_all OK.")
    else:
        raise
PY
  failure_category="data"
  exit 1
fi

echo "[prepare] Computing sha256 for dataset zips and model directory..." >>"$log"
dataset_sha256="$(compute_dataset_bundle_sha256 2>>"$log" || true)"
model_sha256="$(compute_dir_sha256 "$model_resolved_dir" 2>>"$log" || true)"

if [[ -z "$dataset_sha256" ]]; then
  failure_category="data"
  echo "[prepare] ERROR: failed to compute dataset_sha256" >>"$log"
  exit 1
fi
if [[ -z "$model_sha256" ]]; then
  failure_category="model"
  echo "[prepare] ERROR: failed to compute model_sha256" >>"$log"
  exit 1
fi

echo "[prepare] DONE dataset_sha256=$dataset_sha256 model_sha256=$model_sha256" >>"$log"
