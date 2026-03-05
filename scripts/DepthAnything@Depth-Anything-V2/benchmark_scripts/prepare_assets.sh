#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model weights).

Default behavior (offline-friendly):
  - Dataset: copies a single example image from repo `assets/examples/` into `benchmark_assets/dataset/`
  - Model: downloads a public small checkpoint into `benchmark_assets/cache/` then links/copies into `benchmark_assets/model/`

Outputs (always written, even on failure):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Optional:
  --repo-root <path>         Repository root (default: auto)
  --dataset-image <path>     Source image path (default: assets/examples/demo01.jpg)
  --model-url <url>          Checkpoint URL (default: Depth-Anything-V2 metric hypersim small)
  --timeout-sec <sec>        Recorded timeout in results.json (default: 1200)
EOF
}

stage="prepare"
task="download"
framework="unknown"
timeout_sec=1200

repo_root=""
dataset_image="assets/examples/demo01.jpg"
model_url="https://huggingface.co/depth-anything/Depth-Anything-V2-Metric-Hypersim-Small/resolve/main/depth_anything_v2_metric_hypersim_vits.pth?download=true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root) repo_root="${2:-}"; shift 2 ;;
    --dataset-image) dataset_image="${2:-}"; shift 2 ;;
    --model-url) model_url="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$repo_root" ]]; then
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
  repo_root="$(cd "$repo_root" && pwd)"
fi

stage_dir="$repo_root/build_output/prepare"
assets_root="$repo_root/benchmark_assets"
cache_dir="$assets_root/cache"
cache_model_dir="$cache_dir/model"
dataset_dir="$assets_root/dataset"
model_dir="$assets_root/model"

mkdir -p "$stage_dir" "$cache_model_dir" "$dataset_dir" "$model_dir"

log_path="$stage_dir/log.txt"
results_json="$stage_dir/results.json"

: >"$log_path"
exec > >(tee -a "$log_path") 2>&1

status="failure"
skip_reason="unknown"
exit_code=1
failure_category="unknown"
decision_reason=""

git_commit=""
if command -v git >/dev/null 2>&1; then
  git_commit="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)"
fi

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  else
    python - <<PY
import hashlib, pathlib
p = pathlib.Path(${path@Q})
h = hashlib.sha256()
with p.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
  fi
}

download_to() {
  local url="$1"
  local dest="$2"
  local tmp="${dest}.tmp"
  rm -f "$tmp"
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --retry 3 --retry-delay 2 -o "$tmp" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$tmp" "$url"
  else
    python - <<PY
import pathlib, urllib.request
url = ${url@Q}
tmp = pathlib.Path(${tmp@Q})
tmp.parent.mkdir(parents=True, exist_ok=True)
urllib.request.urlretrieve(url, tmp.as_posix())
PY
  fi
  mv "$tmp" "$dest"
}

safe_link_or_copy() {
  local src="$1"
  local dst="$2"
  rm -f "$dst"
  if ln -s "$(realpath "$src")" "$dst" 2>/dev/null; then
    return 0
  fi
  cp -f "$src" "$dst"
}

echo "[prepare] repo_root=$repo_root"
echo "[prepare] dataset_image=$dataset_image"
echo "[prepare] model_url=$model_url"

# Dataset: single image copied into benchmark_assets/dataset/images/
dataset_src="$repo_root/$dataset_image"
dataset_images_dir="$dataset_dir/images"
mkdir -p "$dataset_images_dir"
dataset_dst="$dataset_images_dir/$(basename "$dataset_src")"

if [[ ! -f "$dataset_src" ]]; then
  failure_category="data"
  decision_reason="Default dataset image not found at $dataset_image; provide --dataset-image."
else
  cp -f "$dataset_src" "$dataset_dst"
fi

dataset_sha=""
if [[ -f "$dataset_dst" ]]; then
  dataset_sha="$(sha256_file "$dataset_dst" || true)"
fi

# Model: download into cache, then link/copy into benchmark_assets/model/
model_filename="$(basename "${model_url%%\?*}")"
if [[ -z "$model_filename" || "$model_filename" == "/" ]]; then
  model_filename="model.pth"
fi

cache_model_path="$cache_model_dir/$model_filename"
cache_sha_path="$cache_model_dir/$model_filename.sha256"
model_path="$model_dir/$model_filename"

download_needed=1
if [[ -f "$cache_model_path" && -f "$cache_sha_path" ]]; then
  existing_sha="$(sha256_file "$cache_model_path" 2>/dev/null || true)"
  recorded_sha="$(cat "$cache_sha_path" 2>/dev/null || true)"
  if [[ -n "$existing_sha" && -n "$recorded_sha" && "$existing_sha" == "$recorded_sha" ]]; then
    download_needed=0
    echo "[prepare] Using cached model: $cache_model_path (sha256 match)"
  fi
fi

download_ok=0
if [[ "$download_needed" -eq 1 ]]; then
  echo "[prepare] Downloading model to cache: $cache_model_path"
  if download_to "$model_url" "$cache_model_path"; then
    download_ok=1
  else
    download_ok=0
    echo "[prepare] Download failed."
  fi
else
  download_ok=1
fi

model_sha=""
if [[ -f "$cache_model_path" ]]; then
  model_sha="$(sha256_file "$cache_model_path" 2>/dev/null || true)"
  if [[ -n "$model_sha" ]]; then
    printf '%s' "$model_sha" >"$cache_sha_path"
  fi
fi

if [[ "$download_ok" -eq 0 ]]; then
  if [[ -f "$cache_model_path" && -n "$model_sha" ]]; then
    echo "[prepare] Proceeding with existing cached model despite download failure."
  else
    failure_category="download_failed"
  fi
fi

if [[ -f "$cache_model_path" ]]; then
  safe_link_or_copy "$cache_model_path" "$model_path"
fi

if [[ -f "$dataset_dst" && -f "$model_path" ]]; then
  status="success"
  exit_code=0
  failure_category=""
  decision_reason="Prepared 1-image dataset from repo assets and downloaded public metric-depth small checkpoint for metric_depth/run.py inference."
else
  status="failure"
  exit_code=1
  if [[ "$failure_category" == "unknown" ]]; then
    if [[ ! -f "$dataset_dst" ]]; then
      failure_category="data"
    elif [[ ! -f "$model_path" ]]; then
      failure_category="model"
    else
      failure_category="unknown"
    fi
  fi
fi

STATUS="$status" SKIP_REASON="$skip_reason" EXIT_CODE="$exit_code" STAGE="$stage" TASK="$task" \
TIMEOUT_SEC="$timeout_sec" FRAMEWORK="$framework" DATASET_PATH="$dataset_images_dir" DATASET_SOURCE="repo:$dataset_image" \
DATASET_VERSION="$git_commit" DATASET_SHA256="$dataset_sha" MODEL_PATH="$model_path" MODEL_SOURCE="$model_url" \
MODEL_VERSION="main" MODEL_SHA256="$model_sha" GIT_COMMIT="$git_commit" DECISION_REASON="$decision_reason" \
FAILURE_CATEGORY="$failure_category" LOG_PATH="$log_path" RESULTS_JSON="$results_json" \
  python - <<'PY'
import json
import os
import pathlib
import platform

def tail(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-max_lines:])

results_json = pathlib.Path(os.environ["RESULTS_JSON"])
log_path = pathlib.Path(os.environ["LOG_PATH"])

payload = {
    "status": os.environ["STATUS"],
    "skip_reason": os.environ.get("SKIP_REASON", "unknown"),
    "exit_code": int(os.environ["EXIT_CODE"]),
    "stage": os.environ["STAGE"],
    "task": os.environ["TASK"],
    "command": "bash benchmark_scripts/prepare_assets.sh",
    "timeout_sec": int(os.environ["TIMEOUT_SEC"]),
    "framework": os.environ.get("FRAMEWORK", "unknown"),
    "assets": {
        "dataset": {
            "path": os.environ.get("DATASET_PATH", ""),
            "source": os.environ.get("DATASET_SOURCE", ""),
            "version": os.environ.get("DATASET_VERSION", ""),
            "sha256": os.environ.get("DATASET_SHA256", ""),
        },
        "model": {
            "path": os.environ.get("MODEL_PATH", ""),
            "source": os.environ.get("MODEL_SOURCE", ""),
            "version": os.environ.get("MODEL_VERSION", ""),
            "sha256": os.environ.get("MODEL_SHA256", ""),
        },
    },
    "meta": {
        "python": f"{os.sys.executable} ({platform.python_version()})",
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        },
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", ""),
    "error_excerpt": tail(log_path),
}
results_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

exit "$exit_code"
