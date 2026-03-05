#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (minimal dataset + minimal model) in a reproducible layout.

Default choices (derived from repo README/tests):
  - Dataset: dummy tar+tsv from ml-mdm-matryoshka/tests/test_files
  - Text model: google/flan-t5-small (downloaded to benchmark_assets/cache then linked to benchmark_assets/model)

Outputs (always written, even on failure):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Asset dirs:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Options:
  --python <path>        Override python executable (otherwise resolved from report.json)
  --report-path <path>   Override report.json path (default: /opt/scimlopsbench/report.json)
  --text-model <id>      HF model id (default: google/flan-t5-small)
  --revision <rev>       HF revision (default: main)
  --timeout-sec <sec>    Model download timeout (default: 1200)
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

out_dir="${repo_root}/build_output/prepare"
log_path="${out_dir}/log.txt"
results_json="${out_dir}/results.json"
mkdir -p "$out_dir"
: >"$log_path"
exec > >(tee -a "$log_path") 2>&1

python_bin=""
report_path=""
text_model_id="google/flan-t5-small"
revision="main"
timeout_sec="1200"
orig_args=("$@")

command_str="bash ${repo_root}/benchmark_scripts/prepare_assets.sh"
if [[ ${#orig_args[@]} -gt 0 ]]; then
  command_str+=" $(printf '%q ' "${orig_args[@]}")"
  command_str="${command_str% }"
fi

python_version=""
dataset_path=""
dataset_source=""
dataset_version=""
dataset_sha=""
model_path=""
model_source=""
model_version=""
model_sha=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --text-model) text_model_id="${2:-}"; shift 2 ;;
    --revision) revision="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

git_commit=""
if command -v git >/dev/null 2>&1; then
  git_commit="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)"
fi

write_results() {
  local status="$1" # success|failure|skipped
  local failure_category="$2"
  local exit_code="$3"
  local skip_reason="$4"
  local dataset_path="$5"
  local dataset_source="$6"
  local dataset_version="$7"
  local dataset_sha="$8"
  local model_path="$9"
  local model_source="${10}"
  local model_version="${11}"
  local model_sha="${12}"
  local decision_reason="${13}"
  PREP_OUT_DIR="$out_dir" \
  PREP_STATUS="$status" \
  PREP_SKIP_REASON="$skip_reason" \
  PREP_EXIT_CODE="$exit_code" \
  PREP_COMMAND="$command_str" \
  PREP_TIMEOUT_SEC="$timeout_sec" \
  PREP_DATASET_PATH="$dataset_path" PREP_DATASET_SOURCE="$dataset_source" PREP_DATASET_VERSION="$dataset_version" PREP_DATASET_SHA256="$dataset_sha" \
  PREP_MODEL_PATH="$model_path" PREP_MODEL_SOURCE="$model_source" PREP_MODEL_VERSION="$model_version" PREP_MODEL_SHA256="$model_sha" \
  PREP_PYTHON_BIN="$python_bin" PREP_PYTHON_VERSION="$python_version" \
  PREP_GIT_COMMIT="$git_commit" PREP_DECISION_REASON="$decision_reason" \
  PREP_FAILURE_CATEGORY="$failure_category" \
  python3 - <<'PY'
import json
import os
import pathlib

out_dir = pathlib.Path(os.environ["PREP_OUT_DIR"])
log_path = out_dir / "log.txt"
results_path = out_dir / "results.json"

def tail(path: pathlib.Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

python_bin = os.environ.get("PREP_PYTHON_BIN", "")
python_version = os.environ.get("PREP_PYTHON_VERSION", "")
python_meta = f"{python_bin} ({python_version})" if python_bin and python_version else (python_bin or python_version)

env_keys = [
    "SCIMLOPSBENCH_REPORT",
    "SCIMLOPSBENCH_PYTHON",
    "HF_HOME",
    "HF_HUB_CACHE",
    "HUGGINGFACE_HUB_CACHE",
    "TRANSFORMERS_CACHE",
    "XDG_CACHE_HOME",
    "TORCH_HOME",
    "PIP_CACHE_DIR",
]
env_vars = {k: os.environ.get(k, "") for k in env_keys if os.environ.get(k)}

payload = {
    "status": os.environ.get("PREP_STATUS", "failure"),
    "skip_reason": os.environ.get("PREP_SKIP_REASON", "unknown"),
    "exit_code": int(os.environ.get("PREP_EXIT_CODE", "1") or "1"),
    "stage": "prepare",
    "task": "download",
    "command": os.environ.get("PREP_COMMAND", ""),
    "timeout_sec": int(os.environ.get("PREP_TIMEOUT_SEC", "1200") or "1200"),
    "framework": "unknown",
    "assets": {
        "dataset": {
            "path": os.environ.get("PREP_DATASET_PATH", ""),
            "source": os.environ.get("PREP_DATASET_SOURCE", ""),
            "version": os.environ.get("PREP_DATASET_VERSION", ""),
            "sha256": os.environ.get("PREP_DATASET_SHA256", ""),
        },
        "model": {
            "path": os.environ.get("PREP_MODEL_PATH", ""),
            "source": os.environ.get("PREP_MODEL_SOURCE", ""),
            "version": os.environ.get("PREP_MODEL_VERSION", ""),
            "sha256": os.environ.get("PREP_MODEL_SHA256", ""),
        },
    },
    "meta": {
        "python": python_meta,
        "git_commit": os.environ.get("PREP_GIT_COMMIT", ""),
        "env_vars": env_vars,
        "decision_reason": os.environ.get("PREP_DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("PREP_FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail(log_path),
}
results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

finalize_on_exit() {
  local rc=$?
  if [[ -f "$results_json" ]]; then
    return 0
  fi
  write_results "failure" "unknown" "1" "unknown" "$dataset_path" "$dataset_source" "$dataset_version" "$dataset_sha" \
    "$model_path" "$model_source" "$model_version" "$model_sha" "Unexpected exit (rc=${rc})"
}
trap finalize_on_exit EXIT

resolve_python() {
  if [[ -n "$python_bin" ]]; then
    echo "$python_bin"
    return 0
  fi
  python3 "${repo_root}/benchmark_scripts/runner.py" resolve-python ${report_path:+--report-path "$report_path"}
}

resolved_python="$(resolve_python || true)"
if [[ -z "$resolved_python" ]]; then
  write_results "failure" "missing_report" "1" "not_applicable" "" "" "" "" "" "" "" "" \
    "Could not resolve python from report.json; pass --python or provide /opt/scimlopsbench/report.json"
  exit 1
fi
python_bin="$resolved_python"
python_version="$("$python_bin" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
echo "Using python: $python_bin ($python_version)"

# ------------------------------------------------------------
# Dataset: repo-provided dummy files -> benchmark_assets/dataset/ml_mdm_dummy
# ------------------------------------------------------------
dataset_src_dir="${repo_root}/ml-mdm-matryoshka/tests/test_files"
dataset_dst_dir="${repo_root}/benchmark_assets/dataset/ml_mdm_dummy"
cache_dir="${repo_root}/benchmark_assets/cache"
mkdir -p "${cache_dir}" "${repo_root}/benchmark_assets/dataset" "${repo_root}/benchmark_assets/model"

if [[ ! -d "$dataset_src_dir" ]]; then
  write_results "failure" "data" "1" "not_applicable" "" "" "" "" "" "" "" "" \
    "Expected dummy dataset directory not found: ${dataset_src_dir}"
  exit 1
fi

echo "Preparing dataset from: $dataset_src_dir"
mkdir -p "$dataset_dst_dir"

tar_src="${dataset_src_dir}/images_00000.tar"
if [[ ! -f "$tar_src" ]]; then
  write_results "failure" "data" "1" "not_applicable" "" "" "" "" "" "" "" "" \
    "Missing expected dummy tar file: ${tar_src}"
  exit 1
fi
cp -f "$tar_src" "${dataset_dst_dir}/images_00000.tar"

# Rebuild TSVs with paths relative to ml-mdm-matryoshka working directory.
rel_prefix="../benchmark_assets/dataset/ml_mdm_dummy"
{
  printf 'tar\tfile\tcaption\n'
  printf '%s\t%s\t%s\n' \
    "${rel_prefix}/images_00000.tar" \
    "0000000000.jpg" \
    "Manager in store with TVs, computers, laptops, printers, monitors."
} >"${dataset_dst_dir}/images_00000.tsv"

cat >"${dataset_dst_dir}/sample_training_0.tsv" <<EOF_LIST
filename
${rel_prefix}/images_00000.tsv
EOF_LIST

dataset_tar="${dataset_dst_dir}/images_00000.tar"
dataset_sha="$(DATASET_TAR="$dataset_tar" python3 - <<'PY'
import hashlib
import os
import pathlib

p = pathlib.Path(os.environ["DATASET_TAR"])
h = hashlib.sha256()
with p.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
)"

dataset_path="${dataset_dst_dir}/sample_training_0.tsv"
dataset_source="repo:${dataset_src_dir}"
dataset_version="dummy_test_files"

# ------------------------------------------------------------
# Model: download to cache, then link/copy to benchmark_assets/model/<name>
# ------------------------------------------------------------
hf_home="${cache_dir}/hf_home"
hf_models_cache="${cache_dir}/hf_models"
mkdir -p "$hf_home" "$hf_models_cache" "${cache_dir}/pip" "${cache_dir}/xdg" "${cache_dir}/torch"

export HF_HOME="$hf_home"
export HF_HUB_CACHE="${hf_home}/hub"
export HUGGINGFACE_HUB_CACHE="${hf_home}/hub"
export TRANSFORMERS_CACHE="${hf_home}/hub"
export XDG_CACHE_HOME="${cache_dir}/xdg"
export TORCH_HOME="${cache_dir}/torch"
export PIP_CACHE_DIR="${cache_dir}/pip"

safe_id="$(echo "$text_model_id" | sed 's|/|__|g' | sed 's|[^A-Za-z0-9_.-]|_|g')"
model_cache_dir="${hf_models_cache}/${safe_id}"
model_dst_dir="${repo_root}/benchmark_assets/model/${safe_id}"

echo "Preparing model: ${text_model_id} (revision=${revision})"
echo "Cache dir: ${model_cache_dir}"
echo "Model link dir: ${model_dst_dir}"

prev_model_sha=""
if [[ -f "$results_json" ]]; then
  prev_model_sha="$(PREV_RESULTS_JSON="$results_json" python3 - <<'PY'
import json
import os
import pathlib
p = pathlib.Path(os.environ["PREV_RESULTS_JSON"])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    sha = (((data.get("assets") or {}).get("model") or {}).get("sha256") or "").strip()
    print(sha)
except Exception:
    print("")
PY
)"
fi

download_ok=0
download_err=""
verify_model_dir() {
  local d="$1"
  [[ -d "$d" ]] || return 1
  # Expect at least config.json + some weights/tokenizer artifacts.
  if compgen -G "${d}/config.json" >/dev/null 2>&1; then
    return 0
  fi
  if compgen -G "${d}/*model*.bin" >/dev/null 2>&1; then
    return 0
  fi
  if compgen -G "${d}/*model*.safetensors" >/dev/null 2>&1; then
    return 0
  fi
  if compgen -G "${d}/spiece.model" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

hash_first_model_file() {
  local d="$1"
  local f=""
  if [[ -f "${d}/pytorch_model.bin" ]]; then
    f="${d}/pytorch_model.bin"
  else
    f="$(ls -1 "${d}"/model*.safetensors 2>/dev/null | head -n 1 || true)"
  fi
  if [[ -z "$f" ]]; then
    f="$(ls -1 "${d}"/* 2>/dev/null | head -n 1 || true)"
  fi
  if [[ -z "$f" || ! -f "$f" ]]; then
    echo ""
    return 0
  fi
  MODEL_HASH_FILE="$f" python3 - <<'PY'
import hashlib
import os
import pathlib
p = pathlib.Path(os.environ["MODEL_HASH_FILE"])
h = hashlib.sha256()
with p.open("rb") as fp:
    for chunk in iter(lambda: fp.read(1024*1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
}

skip_download=0
current_cached_sha=""
if verify_model_dir "$model_cache_dir"; then
  current_cached_sha="$(hash_first_model_file "$model_cache_dir")"
  if [[ -n "$prev_model_sha" && -n "$current_cached_sha" && "$prev_model_sha" == "$current_cached_sha" ]]; then
    skip_download=1
    echo "Model cache already present and sha256 matches previous run; skipping download."
  fi
fi

if [[ $skip_download -eq 1 ]]; then
  download_ok=1
  download_err="skipped_sha_match"
else
  set +e
  if command -v timeout >/dev/null 2>&1; then
    HF_REPO_ID="$text_model_id" HF_REVISION="$revision" HF_LOCAL_DIR="$model_cache_dir" \
      timeout "${timeout_sec}s" "$python_bin" - <<'PY'
import os
import sys
from pathlib import Path

repo_id = os.environ["HF_REPO_ID"]
revision = os.environ.get("HF_REVISION") or None
local_dir = Path(os.environ["HF_LOCAL_DIR"])
local_dir.mkdir(parents=True, exist_ok=True)

try:
    from huggingface_hub import snapshot_download
except Exception as e:
    print(f"ERROR: huggingface_hub import failed: {e}", file=sys.stderr)
    sys.exit(3)

try:
    out = snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=str(local_dir),
        local_dir_use_symlinks=False,
        resume_download=True,
    )
    print(f"snapshot_download_returned={out}")
except Exception as e:
    print(f"ERROR: snapshot_download failed: {e}", file=sys.stderr)
    sys.exit(2)
PY
    dl_rc=$?
  else
    HF_REPO_ID="$text_model_id" HF_REVISION="$revision" HF_LOCAL_DIR="$model_cache_dir" \
      "$python_bin" - <<'PY'
import os
import sys
from pathlib import Path

repo_id = os.environ["HF_REPO_ID"]
revision = os.environ.get("HF_REVISION") or None
local_dir = Path(os.environ["HF_LOCAL_DIR"])
local_dir.mkdir(parents=True, exist_ok=True)

try:
    from huggingface_hub import snapshot_download
except Exception as e:
    print(f"ERROR: huggingface_hub import failed: {e}", file=sys.stderr)
    sys.exit(3)

try:
    out = snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=str(local_dir),
        local_dir_use_symlinks=False,
        resume_download=True,
    )
    print(f"snapshot_download_returned={out}")
except Exception as e:
    print(f"ERROR: snapshot_download failed: {e}", file=sys.stderr)
    sys.exit(2)
PY
    dl_rc=$?
  fi
  set -e

  if [[ $dl_rc -eq 0 ]]; then
    download_ok=1
  else
    download_ok=0
    download_err="snapshot_download_failed_rc=${dl_rc}"
  fi
fi

if ! verify_model_dir "$model_cache_dir"; then
  if [[ $download_ok -eq 1 ]]; then
    write_results "failure" "model" "1" "not_applicable" "$dataset_path" "$dataset_source" "$dataset_version" "$dataset_sha" "" \
      "hf:${text_model_id}" "$revision" "" \
      "Downloader indicated success but resolved model directory could not be verified: ${model_cache_dir}"
    exit 1
  fi
  download_failure_category="download_failed"
  if [[ "${download_err}" == *"rc=3"* ]]; then
    download_failure_category="deps"
  elif [[ "${download_err}" == *"rc=124"* ]]; then
    download_failure_category="timeout"
  fi
  write_results "failure" "${download_failure_category}" "1" "not_applicable" "$dataset_path" "$dataset_source" "$dataset_version" "$dataset_sha" "" \
    "hf:${text_model_id}" "$revision" "" \
    "Model download failed and no usable cache found under: ${model_cache_dir} (${download_err})"
  exit 1
fi

rm -rf "$model_dst_dir" 2>/dev/null || true
if ln -s "$model_cache_dir" "$model_dst_dir" 2>/dev/null; then
  echo "Linked model -> $model_dst_dir"
else
  echo "Symlink failed; copying model directory..."
  mkdir -p "$model_dst_dir"
  cp -R "$model_cache_dir"/. "$model_dst_dir"/
fi

if [[ ! -d "$model_dst_dir" ]]; then
  write_results "failure" "model" "1" "not_applicable" "$dataset_path" "$dataset_source" "$dataset_version" "$dataset_sha" "" \
    "hf:${text_model_id}" "$revision" "" \
    "Model link/copy completed but resolved model directory is not present: ${model_dst_dir}"
  exit 1
fi

model_sha="$(hash_first_model_file "$model_cache_dir")"

model_path="$model_dst_dir"
model_source="hf:${text_model_id}"
model_version="$revision"

write_results "success" "unknown" "0" "not_applicable" "$dataset_path" "$dataset_source" "$dataset_version" "$dataset_sha" \
  "$model_path" "$model_source" "$model_version" "$model_sha" \
  "Prepared dummy dataset from repo tests and downloaded HuggingFace text model to a repo-local cache, linking it into benchmark_assets/model for offline reuse."
exit 0
