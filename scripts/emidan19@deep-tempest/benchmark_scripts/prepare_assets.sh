#!/usr/bin/env bash
set -euo pipefail

stage="prepare"
task="download"
timeout_sec="${SCIMLOPSBENCH_PREPARE_TIMEOUT_SEC:-1200}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="${repo_root}/build_output/${stage}"
log_file="${out_dir}/log.txt"
results_json="${out_dir}/results.json"

python_bin="$(command -v python3 || command -v python || true)"

assets_root="${repo_root}/benchmark_assets"
cache_dir="${assets_root}/cache"
dataset_dir="${assets_root}/dataset"
model_dir="${assets_root}/model"

mkdir -p "${out_dir}"
exec >"${log_file}" 2>&1

echo "[prepare] repo_root=${repo_root}"
echo "[prepare] timestamp_utc=$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

git_commit="$(
  git -C "${repo_root}" rev-parse HEAD 2>/dev/null || true
)"

status="failure"
skip_reason="not_applicable"
exit_code=1
failure_category="unknown"
command_str="prepare_assets.sh"
decision_reason=""

dataset_source="repo:examples"
dataset_version="${git_commit:-unknown}"
dataset_sha=""

model_source_default="https://www.fing.edu.uy/owncloud/index.php/s/IaZugHCrw5K1AcB/download"
model_source="${DEEPEMPEST_MODEL_URL:-${model_source_default}}"
model_version="unknown"
model_sha=""
resolved_model_file=""

write_results() {
  local error_excerpt
  error_excerpt="$(tail -n 220 "${log_file}" || true)"

  mkdir -p "$(dirname "${results_json}")"

  if [[ -z "${python_bin}" ]]; then
    # Minimal fallback if python isn't available.
    cat >"${results_json}" <<JSON
{
  "status": "${status}",
  "skip_reason": "${skip_reason}",
  "exit_code": ${exit_code},
  "stage": "${stage}",
  "task": "${task}",
  "command": "${command_str}",
  "timeout_sec": ${timeout_sec},
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "${dataset_dir}", "source": "${dataset_source}", "version": "${dataset_version}", "sha256": "${dataset_sha}"},
    "model": {"path": "${model_dir}", "source": "${model_source}", "version": "${model_version}", "sha256": "${model_sha}"}
  },
  "meta": {
    "python": "",
    "git_commit": "${git_commit}",
    "env_vars": {"DEEPEMPEST_MODEL_URL": "${DEEPEMPEST_MODEL_URL:-}"},
    "decision_reason": "${decision_reason}",
    "resolved_model_file": "${resolved_model_file}",
    "timestamp_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  },
  "failure_category": "${failure_category}",
  "error_excerpt": "See build_output/prepare/log.txt"
}
JSON
    return
  fi

  STATUS="${status}" SKIP_REASON="${skip_reason}" EXIT_CODE="${exit_code}" STAGE="${stage}" TASK="${task}" COMMAND_STR="${command_str}" TIMEOUT_SEC="${timeout_sec}" \
  PYTHON_BIN="${python_bin}" GIT_COMMIT="${git_commit}" DECISION_REASON="${decision_reason}" FAILURE_CATEGORY="${failure_category}" ERROR_EXCERPT="${error_excerpt}" \
  DATASET_PATH="${dataset_dir}" DATASET_SOURCE="${dataset_source}" DATASET_VERSION="${dataset_version}" DATASET_SHA="${dataset_sha}" \
  MODEL_PATH="${model_dir}" MODEL_SOURCE="${model_source}" MODEL_VERSION="${model_version}" MODEL_SHA="${model_sha}" RESOLVED_MODEL_FILE="${resolved_model_file}" \
  "${python_bin}" - <<'PY'
import json
import os
import time
from pathlib import Path

payload = {
  "status": os.environ.get("STATUS", "failure"),
  "skip_reason": os.environ.get("SKIP_REASON", "not_applicable"),
  "exit_code": int(os.environ.get("EXIT_CODE", "1")),
  "stage": os.environ.get("STAGE", "prepare"),
  "task": os.environ.get("TASK", "download"),
  "command": os.environ.get("COMMAND_STR", ""),
  "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "0")),
  "framework": "unknown",
  "assets": {
    "dataset": {
      "path": os.environ.get("DATASET_PATH", ""),
      "source": os.environ.get("DATASET_SOURCE", ""),
      "version": os.environ.get("DATASET_VERSION", ""),
      "sha256": os.environ.get("DATASET_SHA", ""),
    },
    "model": {
      "path": os.environ.get("MODEL_PATH", ""),
      "source": os.environ.get("MODEL_SOURCE", ""),
      "version": os.environ.get("MODEL_VERSION", ""),
      "sha256": os.environ.get("MODEL_SHA", ""),
    },
  },
  "meta": {
    "python": os.environ.get("PYTHON_BIN", ""),
    "git_commit": os.environ.get("GIT_COMMIT", ""),
    "env_vars": {
      "DEEPEMPEST_MODEL_URL": os.environ.get("DEEPEMPEST_MODEL_URL", ""),
      "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
      "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
    },
    "decision_reason": os.environ.get("DECISION_REASON", ""),
    "resolved_model_file": os.environ.get("RESOLVED_MODEL_FILE", ""),
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  },
  "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
  "error_excerpt": os.environ.get("ERROR_EXCERPT", ""),
}

Path("build_output/prepare/results.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

on_exit() {
  local rc="$?"
  if [[ "${rc}" -eq 0 ]]; then
    status="${status:-success}"
    exit_code=0
    if [[ -z "${failure_category}" ]]; then
      failure_category="not_applicable"
    fi
  else
    status="failure"
    exit_code=1
    if [[ -z "${failure_category}" || "${failure_category}" == "not_applicable" ]]; then
      failure_category="unknown"
    fi
  fi
  write_results || true
}
trap on_exit EXIT

fail() {
  local category="$1"
  local msg="$2"
  failure_category="${category}"
  echo "[prepare] ERROR: ${msg}"
  exit 1
}

sha256_cmd=""
if command -v sha256sum >/dev/null 2>&1; then
  sha256_cmd="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
  sha256_cmd="shasum -a 256"
else
  decision_reason="Need sha256 tool to checksum prepared assets."
  fail "deps" "sha256sum/shasum not available in PATH"
fi

mkdir -p "${cache_dir}" "${dataset_dir}" "${model_dir}"
mkdir -p "${cache_dir}/dataset" "${cache_dir}/model"

echo "[prepare] Preparing minimal dataset from ${dataset_source}"

examples_dir="${repo_root}/examples"
if [[ ! -d "${examples_dir}" ]]; then
  decision_reason="This repository includes example PNGs under examples/ used as a tiny offline dataset."
  fail "data" "examples/ directory not found; cannot build minimal dataset."
fi

# Build two tiny paired datasets:
# - mini_1: 1 pair (for CPU / single-GPU one-step)
# - mini_2: 2 pairs (for multi-GPU one-step with per-GPU batch_size=1)
mini1_h="${dataset_dir}/mini_1/H"
mini1_l="${dataset_dir}/mini_1/L"
mini2_h="${dataset_dir}/mini_2/H"
mini2_l="${dataset_dir}/mini_2/L"
mkdir -p "${mini1_h}" "${mini1_l}" "${mini2_h}" "${mini2_l}"

pick_pairs=(
  "Screenshot from 2023-08-29 18-08-19.png|Screenshot from 2023-08-29 18-08-19-gr-tempest_screenshot_05-09-2023_21_07_37.png"
  "Screenshot from 2023-08-29 18-08-27.png|Screenshot from 2023-08-29 18-08-27-gr-tempest_screenshot_05-09-2023_21_10_14.png"
)

missing_pair=0
idx=0
for pair in "${pick_pairs[@]}"; do
  IFS="|" read -r h_name l_name <<<"${pair}"
  h_src="${examples_dir}/${h_name}"
  l_src="${examples_dir}/${l_name}"
  if [[ ! -f "${h_src}" || ! -f "${l_src}" ]]; then
    echo "[prepare] ERROR: Expected example files not found:"
    echo "  H: ${h_src}"
    echo "  L: ${l_src}"
    missing_pair=1
    continue
  fi
  out_base="sample${idx}.png"
  if [[ "${idx}" -eq 0 ]]; then
    cp -f "${h_src}" "${mini1_h}/${out_base}"
    cp -f "${l_src}" "${mini1_l}/${out_base}"
  fi
  cp -f "${h_src}" "${mini2_h}/${out_base}"
  cp -f "${l_src}" "${mini2_l}/${out_base}"
  idx=$((idx + 1))
done

if [[ "${missing_pair}" -ne 0 ]]; then
  decision_reason="Prepare uses repo-provided examples/*.png as a tiny offline dataset."
  fail "data" "One or more required example PNG pairs were missing under examples/."
fi

dataset_manifest="${cache_dir}/dataset/mini_dataset.sha256manifest"
(
  cd "${repo_root}"
  ${sha256_cmd} "benchmark_assets/dataset/mini_1/H/"*.png "benchmark_assets/dataset/mini_1/L/"*.png \
               "benchmark_assets/dataset/mini_2/H/"*.png "benchmark_assets/dataset/mini_2/L/"*.png \
    | awk '{print $1"  "$2}' \
    | sort
) >"${dataset_manifest}"
dataset_sha="$(${sha256_cmd} "${dataset_manifest}" | awk '{print $1}')"
echo "[prepare] dataset_sha256=${dataset_sha}"

echo "[prepare] Preparing minimal model/weights"

# Reuse already-prepared model if present.
if compgen -G "${model_dir}/*" >/dev/null 2>&1; then
  candidate="$(find "${model_dir}" -maxdepth 1 -type f \( -name '*_G.pth' -o -name '*.pth' -o -name '*.pt' -o -name '*.ckpt' -o -name '*.pth.tar' -o -name '*.bin' \) | sort | head -n 1 || true)"
  if [[ -n "${candidate}" ]]; then
    # Detect obviously corrupted zip checkpoints (e.g., truncated downloads).
    candidate_zip_ok=0
    if "${python_bin}" - "${candidate}" >/dev/null 2>&1 <<'PY'; then
import sys
import zipfile
from pathlib import Path

p = Path(sys.argv[1])
with p.open("rb") as f:
    head = f.read(2)

if head == b"PK" and not zipfile.is_zipfile(str(p)):
    raise SystemExit(1)
raise SystemExit(0)
PY
      candidate_zip_ok=1
    fi

    if [[ "${candidate_zip_ok}" -eq 1 ]]; then
      resolved_model_file="${candidate}"
      model_sha="$(${sha256_cmd} "${resolved_model_file}" | awk '{print $1}')"
      echo "[prepare] Reusing existing model file: ${resolved_model_file}"
    else
      echo "[prepare] WARNING: Existing model file looks like a truncated zip checkpoint; ignoring: ${candidate}"
    fi
  fi
fi

if [[ -z "${resolved_model_file}" ]]; then
  downloader=""
  if command -v curl >/dev/null 2>&1; then
    downloader="curl"
  elif command -v wget >/dev/null 2>&1; then
    downloader="wget"
  fi

  if [[ -z "${downloader}" ]]; then
    decision_reason="Model download requires curl or wget for anonymous HTTP(S) download."
    fail "deps" "curl/wget not available in PATH"
  fi

  model_cache_path="${cache_dir}/model/model_download"
  model_cache_tmp_path="${cache_dir}/model/model_download.tmp"
  model_cache_sha_path="${cache_dir}/model/model_download.sha256"
  model_cache_source_path="${cache_dir}/model/model_download.source"

  cached_source=""
  if [[ -f "${model_cache_source_path}" ]]; then
    cached_source="$(cat "${model_cache_source_path}" 2>/dev/null || true)"
  fi

  # If cache exists but is clearly a truncated zipfile, remove it to force a clean download.
  if [[ -s "${model_cache_path}" ]]; then
    if ! "${python_bin}" - "${model_cache_path}" >/dev/null 2>&1 <<'PY'; then
import sys
import zipfile
from pathlib import Path

p = Path(sys.argv[1])
with p.open("rb") as f:
    head = f.read(2)

# Truncated zipfiles often start with 'PK' but lack a central directory.
if head == b"PK" and not zipfile.is_zipfile(str(p)):
    raise SystemExit(1)
raise SystemExit(0)
PY
      echo "[prepare] WARNING: Cached model_download looks like a truncated zip; deleting and re-downloading."
      rm -f "${model_cache_path}" || true
    fi
  fi

  use_cached=0
  if [[ -s "${model_cache_path}" ]]; then
    if [[ -z "${cached_source}" || "${cached_source}" == "${model_source}" ]]; then
      use_cached=1
      echo "[prepare] Found cached model download at ${model_cache_path}; skipping download."
    else
      echo "[prepare] Cached model download exists but source differs:"
      echo "  cached:  ${cached_source}"
      echo "  current: ${model_source}"
      echo "[prepare] Will try to re-download (fallback to cache on failure)."
    fi
  fi

  if [[ "${use_cached}" -eq 0 ]]; then
    echo "[prepare] Downloading model from: ${model_source}"
    rm -f "${model_cache_tmp_path}" || true

    set +e
    if [[ "${downloader}" == "curl" ]]; then
      # Download to a temporary path first so a failed/partial download never poisons the cache.
      curl -fL --retry 3 --retry-delay 2 -C - -o "${model_cache_tmp_path}" "${model_source}"
      dl_rc=$?
    else
      wget -O "${model_cache_tmp_path}" -c "${model_source}"
      dl_rc=$?
    fi
    set -e

    if [[ "${dl_rc}" -ne 0 ]]; then
      rm -f "${model_cache_tmp_path}" || true
      if [[ -s "${model_cache_path}" ]]; then
        echo "[prepare] WARNING: download failed (rc=${dl_rc}) but a previous cached model_download exists; proceeding with cache."
      else
        command_str="${downloader} ${model_source}"
        decision_reason="Attempted anonymous download of the repository-provided pretrained model share link."
        fail "download_failed" "Model download failed (rc=${dl_rc}). If offline, set DEEPEMPEST_MODEL_URL or pre-populate benchmark_assets/model/ with a checkpoint."
      fi
    else
      if [[ ! -s "${model_cache_tmp_path}" ]]; then
        rm -f "${model_cache_tmp_path}" || true
        command_str="${downloader} ${model_source}"
        decision_reason="Download command reported success but produced an empty file."
        fail "download_failed" "Downloaded model file is empty; retry (network issue) or set DEEPEMPEST_MODEL_URL / pre-populate benchmark_assets/model/."
      fi

      mv -f "${model_cache_tmp_path}" "${model_cache_path}"
      printf '%s' "${model_source}" > "${model_cache_source_path}" || true
      echo "[prepare] Download complete: ${model_cache_path}"
    fi
  fi

  if [[ ! -s "${model_cache_path}" ]]; then
    command_str="${downloader} ${model_source}"
    decision_reason="Model download/cache must produce a non-empty file before extraction/validation."
    fail "download_failed" "Missing model cache file: ${model_cache_path}"
  fi

  # If it's a zip, extract under cache/model/extracted and then locate a checkpoint-like file.
  extracted_root="${cache_dir}/model/extracted"
  rm -rf "${extracted_root}" || true
  mkdir -p "${extracted_root}"

  is_zip=0
  if "${python_bin}" - "${model_cache_path}" >/dev/null 2>&1 <<'PY'; then
import sys
import zipfile
raise SystemExit(0 if zipfile.is_zipfile(sys.argv[1]) else 1)
PY
    is_zip=1
  fi

  if [[ "${is_zip}" -eq 1 ]]; then
    # PyTorch checkpoints are often zipfiles internally (torch.save uses a zip-based format).
    # The file served by the repo link is a zipfile checkpoint with entries like:
    #   <prefix>/data.pkl, <prefix>/data/0, ..., <prefix>/version
    # In this case the downloaded file itself is the checkpoint and MUST NOT be extracted.
    torch_zip_is_checkpoint="$(
      "${python_bin}" - <<'PY' "${model_cache_path}" || true
import sys
import zipfile

path = sys.argv[1]
try:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n and not n.endswith("/")]
except Exception:
    print("0")
    raise SystemExit(0)

name_set = set(names)
prefixes = {""}
for n in names:
    parts = n.split("/", 1)
    if len(parts) == 2:
        prefixes.add(parts[0])

def has(prefix: str, leaf: str) -> bool:
    return (f"{prefix}/{leaf}" if prefix else leaf) in name_set

def any_starts(prefix: str, startswith: str) -> bool:
    s = f"{prefix}/{startswith}" if prefix else startswith
    return any(n.startswith(s) for n in names)

is_ckpt = False
for pfx in sorted(prefixes, key=lambda x: (x != "", x)):
    if has(pfx, "data.pkl") and has(pfx, "version") and any_starts(pfx, "data/"):
        is_ckpt = True
        break

print("1" if is_ckpt else "0")
PY
    )"

    if [[ "${torch_zip_is_checkpoint}" == "1" ]]; then
      echo "[prepare] Detected PyTorch zip-checkpoint; using downloaded file directly (no extraction)."
      cp -f "${model_cache_path}" "${model_dir}/pretrained.pth"
      resolved_model_file="${model_dir}/pretrained.pth"
    else
      echo "[prepare] Detected zip archive; extracting."
      if command -v unzip >/dev/null 2>&1; then
        unzip -o "${model_cache_path}" -d "${extracted_root}"
      else
        echo "[prepare] unzip not found; extracting with python zipfile."
        "${python_bin}" - <<'PY' "${model_cache_path}" "${extracted_root}"
import sys
import zipfile
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
dst.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(src) as zf:
    zf.extractall(dst)
PY
      fi

      mapfile -t candidates < <(
        find "${extracted_root}" -type f \( -iname '*_G.pth' -o -iname '*.pth' -o -iname '*.pt' -o -iname '*.ckpt' -o -iname '*.pth.tar' -o -iname '*.bin' \) 2>/dev/null | sort
      )

      if [[ "${#candidates[@]}" -eq 0 ]]; then
        command_str="${downloader} ${model_source}"
        decision_reason="Downloaded archive, then searched under benchmark_assets/cache/model for checkpoint-like artifacts."
        echo "[prepare] Extracted file listing (top 200):"
        find "${extracted_root}" -type f | sort | head -n 200
        fail "model" "No checkpoint-like file (*.pth/*.pt/*.ckpt/*.pth.tar/*.bin) found under ${extracted_root} (downloaded from ${model_source})."
      fi

      selected="$(
        printf '%s\n' "${candidates[@]}" | "${python_bin}" - <<'PY'
import os
import sys

paths = [line.strip() for line in sys.stdin if line.strip()]
def score(p: str) -> tuple:
    base = os.path.basename(p)
    prefer = 1 if base.endswith("_G.pth") else 0
    try:
        size = os.path.getsize(p)
    except OSError:
        size = 0
    return (prefer, size)

best = max(paths, key=score)
print(best)
PY
      )"

      if [[ -z "${selected}" || ! -f "${selected}" ]]; then
        command_str="${downloader} ${model_source}"
        decision_reason="Downloaded archive, but selection of checkpoint candidate failed."
        fail "model" "Checkpoint candidate selection failed under ${extracted_root}."
      fi

      ext=".pth"
      if [[ "${selected}" == *.pth.tar || "${selected}" == *.PTH.TAR ]]; then
        ext=".pth.tar"
      elif [[ "${selected}" == *.ckpt || "${selected}" == *.CKPT ]]; then
        ext=".ckpt"
      elif [[ "${selected}" == *.pt || "${selected}" == *.PT ]]; then
        ext=".pt"
      elif [[ "${selected}" == *.bin || "${selected}" == *.BIN ]]; then
        ext=".bin"
      fi

      cp -f "${selected}" "${model_dir}/pretrained${ext}"
      resolved_model_file="${model_dir}/pretrained${ext}"
    fi
  else
    # Assume it's a raw checkpoint file.
    cp -f "${model_cache_path}" "${model_dir}/pretrained.pth"
    resolved_model_file="${model_dir}/pretrained.pth"
  fi

  if [[ ! -f "${resolved_model_file}" ]]; then
    command_str="${downloader} ${model_source}"
    decision_reason="Model path must be verified after download; refusing to report success without the file."
    fail "model" "Resolved model file missing: ${resolved_model_file}"
  fi

  model_sha="$(${sha256_cmd} "${resolved_model_file}" | awk '{print $1}')"
  echo "[prepare] model_sha256=${model_sha}"
fi

status="success"
exit_code=0
failure_category="not_applicable"
decision_reason="Dataset: use repo-provided examples/*.png as a tiny offline paired dataset. Model: download repository-provided pretrained checkpoint share link (or reuse cached benchmark_assets/model/ checkpoint)."

echo "[prepare] Wrote ${results_json}"
exit 0
