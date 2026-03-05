#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

STAGE="prepare"
TASK="download"
OUT_DIR="${REPO_ROOT}/build_output/${STAGE}"
LOG_PATH="${OUT_DIR}/log.txt"
RESULTS_PATH="${OUT_DIR}/results.json"

CACHE_DIR="${REPO_ROOT}/benchmark_assets/cache"
DATASET_DIR="${REPO_ROOT}/benchmark_assets/dataset"
MODEL_DIR="${REPO_ROOT}/benchmark_assets/model"

TIMEOUT_SEC="${SCIMLOPSBENCH_PREPARE_TIMEOUT_SEC:-1200}"

mkdir -p "${OUT_DIR}" "${CACHE_DIR}" "${DATASET_DIR}" "${MODEL_DIR}"
: >"${LOG_PATH}"

exec > >(tee -a "${LOG_PATH}") 2>&1

python_bin_for_json() {
  if command -v python3 >/dev/null 2>&1; then
    echo python3
  else
    echo python
  fi
}

sha256_file() {
  local f="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$f" | awk '{print $1}'
  else
    "$(python_bin_for_json)" - <<PY
import hashlib
import pathlib
p=pathlib.Path(r"""$f""")
h=hashlib.sha256()
with p.open("rb") as fp:
    for chunk in iter(lambda: fp.read(1024*1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
  fi
}

read_prev_sha() {
  local kind="$1" # dataset|model
  local key="$2"  # sha256
  local prev="${REPO_ROOT}/build_output/prepare/results.json"
  if [[ ! -f "${prev}" ]]; then
    echo ""
    return 0
  fi
  "$(python_bin_for_json)" - <<'PY' "$prev" "$kind" "$key"
import json, sys
path, kind, key = sys.argv[1:]
try:
    obj = json.load(open(path, "r", encoding="utf-8"))
    print((obj.get("assets", {}).get(kind, {}) or {}).get(key, "") or "")
except Exception:
    print("")
PY
}

download_to_cache() {
  local url="$1"
  local dest="$2"
  local label="$3"

  mkdir -p "$(dirname "$dest")"

  if command -v curl >/dev/null 2>&1; then
    echo "[prepare] downloading (${label}) via curl: ${url}" >&2
    if ! curl -L --fail --retry 2 --connect-timeout 10 --max-time 600 -o "$dest.tmp" "$url"; then
      rm -f "$dest.tmp" >/dev/null 2>&1 || true
      return 1
    fi
    mv "$dest.tmp" "$dest"
    return 0
  fi
  if command -v wget >/dev/null 2>&1; then
    echo "[prepare] downloading (${label}) via wget: ${url}" >&2
    if ! wget -q -O "$dest.tmp" "$url"; then
      rm -f "$dest.tmp" >/dev/null 2>&1 || true
      return 1
    fi
    mv "$dest.tmp" "$dest"
    return 0
  fi

  echo "[prepare] no curl/wget available for download: ${label}" >&2
  return 1
}

maybe_fetch() {
  local url="$1"
  local cache_path="$2"
  local label="$3"
  local prev_sha="$4"

  if [[ -s "$cache_path" ]]; then
    local cur_sha
    cur_sha="$(sha256_file "$cache_path")"
    if [[ -n "$prev_sha" && "$cur_sha" == "$prev_sha" ]]; then
      echo "[prepare] cache hit (${label}) sha256=${cur_sha}" >&2
      echo "$cur_sha"
      return 0
    fi
    if [[ -z "$prev_sha" ]]; then
      echo "[prepare] cache present (${label}) sha256=${cur_sha}" >&2
      echo "$cur_sha"
      return 0
    fi
    echo "[prepare] cache sha mismatch (${label}) prev=${prev_sha} cur=${cur_sha}; re-downloading" >&2
  fi

  if download_to_cache "$url" "$cache_path" "$label"; then
    local new_sha
    new_sha="$(sha256_file "$cache_path")"
    echo "[prepare] downloaded (${label}) sha256=${new_sha}" >&2
    echo "$new_sha"
    return 0
  fi

  if [[ -s "$cache_path" ]]; then
    local fallback_sha
    fallback_sha="$(sha256_file "$cache_path")"
    echo "[prepare] download failed but cache exists (${label}); proceeding offline sha256=${fallback_sha}" >&2
    echo "$fallback_sha"
    return 0
  fi

  echo "[prepare] download failed and no cache available (${label})" >&2
  return 1
}

status="failure"
skip_reason="unknown"
failure_category="unknown"
exit_code=1

dataset_path=""
dataset_source=""
dataset_version=""
dataset_sha256=""

model_path=""
model_source=""
model_version=""
model_sha256=""

decision_reason="Use Detectron2 official mini COCO val2017_100 dataset + MSRA R-50 backbone weights to enable a fully-downloadable 1-step training run via det/tools/train_net.py."

command_str="bash benchmark_scripts/prepare_assets.sh"

echo "[prepare] repo_root=${REPO_ROOT}"
echo "[prepare] cache_dir=${CACHE_DIR}"
echo "[prepare] dataset_dir=${DATASET_DIR}"
echo "[prepare] model_dir=${MODEL_DIR}"
echo "[prepare] timeout_sec=${TIMEOUT_SEC}"

prev_dataset_sha="$(read_prev_sha dataset sha256)"
prev_model_sha="$(read_prev_sha model sha256)"

finalize() {
  local rc="${1:-0}"

  if [[ "${status}" != "success" ]]; then
    status="failure"
    skip_reason="unknown"
    exit_code=1
    if [[ -z "${failure_category}" || "${failure_category}" == "unknown" ]]; then
      failure_category="unknown"
    fi
  else
    exit_code=0
  fi

  local git_commit
  git_commit="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || true)"
  local pyver
  pyver="$("$(python_bin_for_json)" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || true)"

  "$(python_bin_for_json)" - <<PY
import json, os
log_path = r"""${LOG_PATH}"""
try:
  tail = open(log_path, "r", encoding="utf-8", errors="replace").read().splitlines()[-240:]
  error_excerpt = "\\n".join(tail).strip()
except Exception:
  error_excerpt = ""
payload = {
  "status": "${status}",
  "skip_reason": "${skip_reason}",
  "exit_code": ${exit_code},
  "stage": "${STAGE}",
  "task": "${TASK}",
  "command": "${command_str}",
  "timeout_sec": ${TIMEOUT_SEC},
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "${dataset_path}", "source": "${dataset_source}", "version": "${dataset_version}", "sha256": "${dataset_sha256}"},
    "model": {"path": "${model_path}", "source": "${model_source}", "version": "${model_version}", "sha256": "${model_sha256}"},
  },
  "meta": {
    "python": "${pyver}",
    "git_commit": "${git_commit}",
    "env_vars": {
      "DETECTRON2_DATASETS": os.environ.get("DETECTRON2_DATASETS",""),
      "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT",""),
      "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON",""),
    },
    "decision_reason": "${decision_reason}",
  },
  "failure_category": "${failure_category}",
  "error_excerpt": error_excerpt,
}
out_path = r"""${RESULTS_PATH}"""
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
  json.dump(payload, f, indent=2)
PY

  exit "${exit_code}"
}

trap 'rc=$?; set +e; finalize "$rc"' EXIT

COCO_BASE="https://dl.fbaipublicfiles.com/detectron2/annotations/coco"
COCO_TARBALL_URL="${COCO_BASE}/val2017_100.tgz"
COCO_INSTANCES_URL="${COCO_BASE}/instances_val2017_100.json"

R50_URL="https://dl.fbaipublicfiles.com/detectron2/ImageNetPretrained/MSRA/R-50.pkl"

COCO_TARBALL_CACHE="${CACHE_DIR}/coco/val2017_100.tgz"
COCO_INSTANCES_CACHE="${CACHE_DIR}/coco/instances_val2017_100.json"
R50_CACHE="${CACHE_DIR}/detectron2/R-50.pkl"

COCO_ROOT="${DATASET_DIR}/coco"
COCO_IMAGES_DIR="${COCO_ROOT}/val2017"
COCO_ANN_DIR="${COCO_ROOT}/annotations"
COCO_INSTANCES_DST="${COCO_ANN_DIR}/instances_val2017_100.json"

R50_DST_DIR="${MODEL_DIR}/detectron2"
R50_DST="${R50_DST_DIR}/R-50.pkl"

mkdir -p "${COCO_ANN_DIR}" "${R50_DST_DIR}"

echo "[prepare] downloading dataset artifacts..."
dataset_source="${COCO_TARBALL_URL} ; ${COCO_INSTANCES_URL}"
dataset_version="coco_val2017_100"

dataset_tar_sha="$(maybe_fetch "${COCO_TARBALL_URL}" "${COCO_TARBALL_CACHE}" "coco_val2017_100_tgz" "${prev_dataset_sha}")" || {
  failure_category="download_failed"
  exit 1
}
dataset_inst_sha="$(maybe_fetch "${COCO_INSTANCES_URL}" "${COCO_INSTANCES_CACHE}" "instances_val2017_100_json" "")" || {
  failure_category="download_failed"
  exit 1
}

dataset_sha256="${dataset_tar_sha}"

if [[ ! -d "${COCO_IMAGES_DIR}" || -z "$(ls -A "${COCO_IMAGES_DIR}" 2>/dev/null || true)" ]]; then
  echo "[prepare] extracting ${COCO_TARBALL_CACHE} -> ${COCO_ROOT}"
  tar xzf "${COCO_TARBALL_CACHE}" -C "${COCO_ROOT}"
fi

echo "[prepare] installing annotation json -> ${COCO_INSTANCES_DST}"
cp -f "${COCO_INSTANCES_CACHE}" "${COCO_INSTANCES_DST}"

if [[ ! -d "${COCO_IMAGES_DIR}" ]]; then
  echo "[prepare] expected dataset images directory not found: ${COCO_IMAGES_DIR}" >&2
  failure_category="data"
  exit_code=1
else
  dataset_path="${DATASET_DIR}"
fi

echo "[prepare] downloading model weights..."
model_source="${R50_URL}"
model_version="detectron2_ImageNetPretrained_MSRA_R-50"

model_sha256="$(maybe_fetch "${R50_URL}" "${R50_CACHE}" "detectron2_R-50_pkl" "${prev_model_sha}")" || {
  failure_category="download_failed"
  exit 1
}
cp -f "${R50_CACHE}" "${R50_DST}"

if [[ ! -f "${R50_DST}" ]]; then
  echo "[prepare] expected model file not found: ${R50_DST}" >&2
  failure_category="model"
  exit_code=1
else
  model_path="${R50_DST}"
fi

if [[ -n "${dataset_path}" && -n "${model_path}" ]]; then
  status="success"
  skip_reason="not_applicable"
  failure_category=""
  exit_code=0
fi
