#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

stage_out="$REPO_ROOT/build_output/prepare"
mkdir -p "$stage_out"
log_path="$stage_out/log.txt"
results_path="$stage_out/results.json"

exec > >(tee "$log_path") 2>&1

status="failure"
failure_category="unknown"
skip_reason="unknown"
stage_exit_code=1

report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
cli_python=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --python requires a value" >&2
        exit 2
      fi
      cli_python="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--python /path/to/python]" >&2
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      echo "Usage: $0 [--python /path/to/python]" >&2
      exit 2
      ;;
  esac
done

resolve_python() {
  if [[ -n "$cli_python" ]]; then
    echo "$cli_python|cli:--python"
    return 0
  fi
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    echo "${SCIMLOPSBENCH_PYTHON}|env:SCIMLOPSBENCH_PYTHON"
    return 0
  fi
  if [[ ! -f "$report_path" ]]; then
    return 1
  fi
  python - <<'PY' "$report_path"
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
data = json.loads(p.read_text(encoding="utf-8"))
v = data.get("python_path")
if not isinstance(v, str) or not v.strip():
    raise SystemExit(2)
print(v.strip() + "|report:python_path")
PY
}

resolved_python=""
python_source="unknown"
python_warning=""
resolved_line=""
if resolved_line="$(resolve_python 2>/dev/null)"; then
  IFS="|" read -r resolved_python python_source <<<"$resolved_line"
  if [[ -z "$resolved_python" ]]; then
    echo "ERROR: resolved python is empty" >&2
    failure_category="missing_report"
    status="failure"
    stage_exit_code=1
  elif [[ ! -x "$resolved_python" ]]; then
    python_warning="python_path not executable; falling back to python from PATH"
    resolved_python="python"
    python_source="PATH:fallback"
  fi
else
  echo "ERROR: cannot resolve python (missing/invalid report.json and no --python/SCIMLOPSBENCH_PYTHON)" >&2
  failure_category="missing_report"
  status="failure"
  stage_exit_code=1
fi

cache_root="$REPO_ROOT/benchmark_assets/cache"
dataset_root="$REPO_ROOT/benchmark_assets/dataset"
model_root="$REPO_ROOT/benchmark_assets/model"
mkdir -p "$cache_root" "$dataset_root" "$model_root"

export HF_HOME="$cache_root/huggingface"
export HF_HUB_CACHE="$HF_HOME/hub"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export XDG_CACHE_HOME="$cache_root/xdg"
export TORCH_HOME="$cache_root/torch"

offline=0
if [[ "${SCIMLOPSBENCH_OFFLINE:-}" == "1" || "${HF_HUB_OFFLINE:-}" == "1" ]]; then
  offline=1
  export HF_HUB_OFFLINE=1
fi

echo "== Prepare stage =="
echo "resolved_python=$resolved_python"
echo "python_source=$python_source"
echo "report_path=$report_path"
echo "cache_root=$cache_root"
echo "offline=$offline"
if [[ -n "$python_warning" ]]; then
  echo "WARNING: $python_warning"
fi

ragtruth_base_url="${BENCH_RAGTRUTH_BASE_URL:-https://raw.githubusercontent.com/ParticleMedia/RAGTruth/main/dataset}"
ragtruth_version="${BENCH_RAGTRUTH_VERSION:-ParticleMedia/RAGTruth@main}"
ragtruth_cache_dir="$cache_root/dataset/ragtruth"
ragtruth_raw_dir="$dataset_root/ragtruth/raw"
ragtruth_out_dir="$dataset_root/ragtruth"
mkdir -p "$ragtruth_cache_dir" "$ragtruth_raw_dir" "$ragtruth_out_dir"

download_file() {
  local url="$1"
  local dst="$2"
  echo "Downloading: $url -> $dst"
  if [[ $offline -eq 1 ]]; then
    echo "Offline mode: skipping network download attempt for $url"
    return 1
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dst"
  else
    "$resolved_python" - <<'PY' "$url" "$dst"
import sys, urllib.request
url, dst = sys.argv[1], sys.argv[2]
urllib.request.urlretrieve(url, dst)
PY
  fi
}

ragtruth_response="$ragtruth_cache_dir/response.jsonl"
ragtruth_source="$ragtruth_cache_dir/source_info.jsonl"

if [[ "$failure_category" == "missing_report" ]]; then
  echo "Skipping dataset download/preprocess/model download due to missing_report (no benchmark python resolved)."
else

ragtruth_ok=0
if [[ -f "$ragtruth_response" && -f "$ragtruth_source" ]]; then
  ragtruth_ok=1
fi

if [[ $ragtruth_ok -eq 0 ]]; then
  set +e
  download_file "$ragtruth_base_url/response.jsonl" "$ragtruth_response"
  r1=$?
  download_file "$ragtruth_base_url/source_info.jsonl" "$ragtruth_source"
  r2=$?
  set -e
  if [[ $r1 -ne 0 || $r2 -ne 0 ]]; then
    if [[ -f "$ragtruth_response" && -f "$ragtruth_source" ]]; then
      echo "WARNING: download failed but cache exists; proceeding with offline reuse."
    else
      echo "ERROR: failed to download RAGTruth and no cache present." >&2
      failure_category="download_failed"
      status="failure"
      stage_exit_code=1
    fi
  fi
fi

# Copy cache files into dataset directory (best-effort).
cp -f "$ragtruth_response" "$ragtruth_raw_dir/response.jsonl" 2>/dev/null || true
cp -f "$ragtruth_source" "$ragtruth_raw_dir/source_info.jsonl" 2>/dev/null || true

fi

ragtruth_data_json="$ragtruth_out_dir/ragtruth_data.json"
ragtruth_empty_json="$dataset_root/ragtruth_empty.json"
ragbench_min_json="$dataset_root/ragbench_min.json"

model_id="${BENCH_MODEL_ID:-jhu-clsp/ettin-encoder-17m}"
model_revision="${BENCH_MODEL_REVISION:-main}"
model_cache_dir="$cache_root/model/${model_id//\//__}"
model_link_dir="$model_root/${model_id//\//__}"
model_download_reported_path=""

dataset_sha256="unknown"
model_sha256="unknown"
resolved_model_dir=""

decision_reason="Downloaded RAGTruth raw files from GitHub (per README), created a 2-sample RAGBench-format mini JSON for 1-step training, and downloaded a minimal HF encoder checkpoint for fine-tuning."

if [[ "$failure_category" != "missing_report" ]]; then
  # Reuse optimization: if previous prepare results exist and sha256 matches, skip rebuild.
  prev_prepare="$results_path"
  prev_dataset_sha=""
  prev_model_sha=""
  if [[ -f "$prev_prepare" ]]; then
    prev_dataset_sha="$(
      python - <<'PY' "$prev_prepare" 2>/dev/null || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print((data.get("assets", {}).get("dataset", {}) or {}).get("sha256", "") or "")
except Exception:
    print("")
PY
    )"
    prev_model_sha="$(
      python - <<'PY' "$prev_prepare" 2>/dev/null || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print((data.get("assets", {}).get("model", {}) or {}).get("sha256", "") or "")
except Exception:
    print("")
PY
    )"
  fi

  if [[ -f "$ragbench_min_json" ]]; then
    dataset_sha256="$("$resolved_python" - <<'PY' "$ragbench_min_json"
import hashlib, sys
from pathlib import Path
p = Path(sys.argv[1])
h = hashlib.sha256()
h.update(p.read_bytes())
print(h.hexdigest())
PY
    )"
  fi

  if [[ -n "$prev_dataset_sha" && "$dataset_sha256" == "$prev_dataset_sha" && -f "$ragbench_min_json" && -f "$ragtruth_empty_json" ]]; then
    echo "Dataset already prepared (sha256 match); skipping dataset rebuild."
  else
    echo "Preprocessing RAGTruth -> $ragtruth_data_json"
    set +e
    "$resolved_python" lettucedetect/preprocess/preprocess_ragtruth.py --input_dir "$ragtruth_raw_dir" --output_dir "$ragtruth_out_dir"
    prep_ec=$?
    set -e
    if [[ $prep_ec -ne 0 || ! -f "$ragtruth_data_json" ]]; then
      echo "ERROR: failed to preprocess RAGTruth (exit=$prep_ec)" >&2
      failure_category="data"
      status="failure"
      stage_exit_code=1
    else
      echo "Creating ragbench_min.json + ragtruth_empty.json"
      "$resolved_python" - <<'PY' "$ragtruth_data_json" "$ragbench_min_json" "$ragtruth_empty_json"
import json
import sys
from pathlib import Path

ragtruth_path = Path(sys.argv[1])
ragbench_min = Path(sys.argv[2])
ragtruth_empty = Path(sys.argv[3])

data = json.loads(ragtruth_path.read_text(encoding="utf-8"))
train = [s for s in data if isinstance(s, dict) and s.get("split") == "train"]
if len(train) < 2:
    raise SystemExit("Need at least 2 train samples in ragtruth_data.json to build mini dataset")

sample_train = dict(train[0])
sample_dev = dict(train[1])

sample_train["split"] = "train"
sample_dev["split"] = "dev"

# Mark dataset as ragbench to ensure scripts/train.py uses split as-is for dev samples.
sample_train["dataset"] = "ragbench"
sample_dev["dataset"] = "ragbench"

ragbench_min.write_text(json.dumps([sample_train, sample_dev], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
ragtruth_empty.write_text("[]\n", encoding="utf-8")
print(f"Wrote {ragbench_min} and {ragtruth_empty}")
PY
      dataset_sha256="$("$resolved_python" - <<'PY' "$ragbench_min_json"
import hashlib, sys
from pathlib import Path
p = Path(sys.argv[1])
h = hashlib.sha256()
h.update(p.read_bytes())
print(h.hexdigest())
PY
      )"
    fi
  fi

  # Model download / reuse.
  if [[ -d "$model_link_dir" ]]; then
    resolved_model_dir="$model_link_dir"
  elif [[ -d "$model_cache_dir" ]]; then
    resolved_model_dir="$model_cache_dir"
  fi

  if [[ -n "$resolved_model_dir" ]]; then
    model_sha256="$("$resolved_python" - <<'PY' "$resolved_model_dir"
import hashlib, sys
from pathlib import Path

root = Path(sys.argv[1])
files = []
for p in root.rglob("*"):
    if p.is_file():
        files.append(p)
files = sorted(files, key=lambda p: p.relative_to(root).as_posix())

manifest = []
for p in files:
    rel = p.relative_to(root).as_posix()
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    manifest.append(f"{rel}\t{p.stat().st_size}\t{h.hexdigest()}")

tree = hashlib.sha256()
tree.update("\n".join(manifest).encode("utf-8"))
print(tree.hexdigest())
PY
    )"
  fi

  if [[ -n "$prev_model_sha" && "$model_sha256" == "$prev_model_sha" && -n "$resolved_model_dir" ]]; then
    echo "Model already prepared (sha256 match); skipping model download."
  else
    echo "Downloading model snapshot: $model_id (revision=$model_revision) -> $model_cache_dir"
    mkdir -p "$model_cache_dir"
    set +e
    model_download_reported_path="$(
      "$resolved_python" - <<'PY' "$model_id" "$model_revision" "$model_cache_dir" | tee /dev/stderr
import os
import sys
from pathlib import Path

model_id = sys.argv[1]
revision = sys.argv[2]
local_dir = Path(sys.argv[3])

try:
    from huggingface_hub import snapshot_download  # type: ignore
except Exception as e:
    raise SystemExit(f"huggingface_hub not available: {e!r}")

token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
offline = (os.environ.get("SCIMLOPSBENCH_OFFLINE") == "1") or (os.environ.get("HF_HUB_OFFLINE") == "1")

path = snapshot_download(
    repo_id=model_id,
    revision=revision,
    local_dir=str(local_dir),
    local_dir_use_symlinks=False,
    token=token,
    local_files_only=offline,
)
print(path)
PY
    )"
    dl_ec=$?
    set -e
    if [[ $dl_ec -ne 0 ]]; then
      if rg -n "huggingface_hub not available" "$log_path" >/dev/null 2>&1; then
        failure_category="deps"
      elif rg -n "401|403|Unauthorized|forbidden|token" "$log_path" >/dev/null 2>&1; then
        failure_category="auth_required"
      else
        failure_category="download_failed"
      fi
      status="failure"
      stage_exit_code=1
    else
      # Prefer the downloader-reported local directory path (do not assume hub cache layout).
      model_download_reported_path="$(echo "$model_download_reported_path" | tail -n 1 | tr -d '\r' || true)"
      resolved_download_dir="${model_download_reported_path:-$model_cache_dir}"
      resolved_model_dir="$resolved_download_dir"
      rm -f "$model_link_dir" || true
      ln -s "$resolved_download_dir" "$model_link_dir" || true
      resolved_model_dir="$model_link_dir"
      if [[ ! -d "$resolved_model_dir" ]]; then
        echo "ERROR: model download reported success but resolved dir missing: $resolved_model_dir" >&2
        failure_category="model"
        status="failure"
        stage_exit_code=1
      else
        model_sha256="$("$resolved_python" - <<'PY' "$resolved_model_dir"
import hashlib, sys
from pathlib import Path

root = Path(sys.argv[1])
files = []
for p in root.rglob("*"):
    if p.is_file():
        files.append(p)
files = sorted(files, key=lambda p: p.relative_to(root).as_posix())

manifest = []
for p in files:
    rel = p.relative_to(root).as_posix()
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    manifest.append(f"{rel}\t{p.stat().st_size}\t{h.hexdigest()}")

tree = hashlib.sha256()
tree.update("\n".join(manifest).encode("utf-8"))
print(tree.hexdigest())
PY
        )"
      fi
    fi
  fi

  # Final success gating: dataset + model must exist.
  if [[ -f "$ragbench_min_json" && -f "$ragtruth_empty_json" && -n "$resolved_model_dir" && -d "$resolved_model_dir" ]]; then
    status="success"
    stage_exit_code=0
    failure_category="unknown"
  else
    status="failure"
    stage_exit_code=1
    if [[ "$failure_category" == "unknown" ]]; then
      if [[ ! -f "$ragbench_min_json" || ! -f "$ragtruth_empty_json" ]]; then
        failure_category="data"
      elif [[ -z "$resolved_model_dir" || ! -d "$resolved_model_dir" ]]; then
        failure_category="model"
      fi
    fi
  fi
fi

# Always write results.json (even on failure).
python - <<'PY' \
  "$results_path" "$status" "$stage_exit_code" "$failure_category" "$skip_reason" \
  "$resolved_python" "$report_path" "$python_source" \
  "$ragbench_min_json" "$dataset_sha256" "$ragtruth_version" "$ragtruth_base_url" \
  "$resolved_model_dir" "$model_sha256" "$model_id" "$model_revision" "$model_download_reported_path" \
  "$decision_reason" "$log_path" "$python_warning"
import json
import os
import subprocess
import sys
from pathlib import Path

(
    results_path,
    status,
    exit_code,
    failure_category,
    skip_reason,
    resolved_python,
    report_path,
    python_source,
    dataset_path,
    dataset_sha256,
    dataset_version,
    dataset_source,
    model_path,
    model_sha256,
    model_id,
    model_revision,
    model_download_reported_path,
    decision_reason,
    log_path,
    python_warning,
) = sys.argv[1:]

def git_commit() -> str:
    try:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], check=False, capture_output=True, text=True)
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""

def tail(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    return "\n".join(lines[-max_lines:])

payload = {
    "status": status,
    "skip_reason": skip_reason,
    "exit_code": int(exit_code),
    "stage": "prepare",
    "task": "download",
    "command": "benchmark_scripts/prepare_assets.sh",
    "timeout_sec": 1200,
    "framework": "unknown",
    "assets": {
        "dataset": {
            "path": dataset_path,
            "source": dataset_source,
            "version": dataset_version,
            "sha256": dataset_sha256,
        },
        "model": {
            "path": model_path,
            "source": f"hf:{model_id}",
            "version": model_revision,
            "sha256": model_sha256,
        },
    },
    "meta": {
        "python": resolved_python or sys.executable,
        "git_commit": git_commit(),
        "env_vars": {
            k: ("***REDACTED***" if k in {"HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "OPENAI_API_KEY"} else v)
            for k, v in os.environ.items()
            if k in {
                "CUDA_VISIBLE_DEVICES",
                "HF_HOME",
                "HF_HUB_CACHE",
                "HUGGINGFACE_HUB_CACHE",
                "TRANSFORMERS_CACHE",
                "XDG_CACHE_HOME",
                "TORCH_HOME",
                "SCIMLOPSBENCH_REPORT",
                "SCIMLOPSBENCH_PYTHON",
                "HF_TOKEN",
                "HUGGINGFACE_HUB_TOKEN",
                "OPENAI_API_KEY",
            }
        },
        "decision_reason": decision_reason,
        "report_path": report_path,
        "python_source": python_source,
        "python_warning": python_warning,
        "ragtruth_base_url": dataset_source,
        "model_id": model_id,
        "model_download_reported_path": model_download_reported_path,
    },
    "failure_category": failure_category,
    "error_excerpt": tail(Path(log_path), max_lines=220),
}

Path(results_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

exit "$stage_exit_code"
