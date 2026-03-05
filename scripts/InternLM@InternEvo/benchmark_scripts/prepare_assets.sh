#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets:
  - Download a small, public dataset sample (Stanford Alpaca JSON)
  - Download a minimal, public model asset (InternLM2 SentencePiece tokenizer.model)
  - Tokenize the dataset into InternEvo tokenized bin/meta format

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Assets written under:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Options:
  --out-dir <path>        Default: build_output/prepare
  --report-path <path>    Default: /opt/scimlopsbench/report.json (or $SCIMLOPSBENCH_REPORT)
  --python <path>         Override python interpreter for tokenization steps
  --max-examples <n>      Default: 64 (subsample Alpaca to reduce prep time)
EOF
}

out_dir="build_output/prepare"
report_path=""
python_override=""
max_examples="64"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out-dir) out_dir="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_override="${2:-}"; shift 2 ;;
    --max-examples) max_examples="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p "$out_dir"
log_txt="$out_dir/log.txt"
results_json="$out_dir/results.json"
: >"$log_txt"
exec > >(tee -a "$log_txt") 2>&1

stage_status="failure"
stage_exit_code=1
failure_category="unknown"
skip_reason="not_applicable"
decision_reason="Use InternEvo's documented Alpaca tokenization flow (doc/en/usage.md) with a small deterministic sample, and the documented InternLM2 tokenizer.model from Hugging Face."
command_str="bash benchmark_scripts/prepare_assets.sh --out-dir ${out_dir} --max-examples ${max_examples}"

dataset_source="https://raw.githubusercontent.com/tatsu-lab/stanford_alpaca/main/alpaca_data.json"
dataset_version="stanford_alpaca@main"
model_source="https://huggingface.co/internlm/internlm2-7b/resolve/main/tokenizer.model"
model_version="internlm/internlm2-7b@main"

assets_root="$REPO_ROOT/benchmark_assets"
cache_root="$assets_root/cache"
dataset_root="$assets_root/dataset"
model_root="$assets_root/model"
mkdir -p "$cache_root/dataset" "$cache_root/model" "$cache_root/pip" "$dataset_root" "$model_root"

export PIP_CACHE_DIR="$cache_root/pip"
export HF_HOME="$cache_root/hf"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export HF_DATASETS_CACHE="$HF_HOME/datasets"
export TORCH_HOME="$cache_root/torch"
mkdir -p "$HF_HOME" "$TRANSFORMERS_CACHE" "$HF_DATASETS_CACHE" "$TORCH_HOME"

have_rg=0
if command -v rg >/dev/null 2>&1; then
  have_rg=1
fi

file_has_re() {
  local pattern="$1"
  local file="$2"
  if [[ "$have_rg" -eq 1 ]]; then
    rg -n "$pattern" "$file" >/dev/null 2>&1
  else
    grep -E -n "$pattern" "$file" >/dev/null 2>&1
  fi
}

resolve_python() {
  local sys_py="${PYTHON:-python}"
  local args=(--stage prepare --task download --out-dir "$out_dir" --print-python)
  if [[ -n "$python_override" ]]; then
    args+=(--python "$python_override")
  fi
  if [[ -n "$report_path" ]]; then
    args+=(--report-path "$report_path")
  fi
  "$sys_py" "$REPO_ROOT/benchmark_scripts/runner.py" "${args[@]}"
}

python_bin="$(resolve_python 2>/dev/null || true)"
if [[ -z "$python_bin" ]]; then
  failure_category="missing_report"
  stage_status="failure"
  stage_exit_code=1
  echo "[prepare] failed to resolve python (missing/invalid report and no --python override)" >&2
else
  echo "[prepare] resolved python: $python_bin"
  stage_status="success"
  stage_exit_code=0
  failure_category="not_applicable"
fi

download_to() {
  local url="$1"
  local dest="$2"
  local tmp="${dest}.tmp"
  local sha_sidecar="${dest}.sha256"

  if [[ -s "$dest" ]]; then
    if [[ -s "$sha_sidecar" ]]; then
      local recorded current
      recorded="$(head -n 1 "$sha_sidecar" | awk '{print $1}')"
      current="$(sha256_of "$dest" 2>/dev/null || true)"
      if [[ -n "$current" && "$recorded" == "$current" ]]; then
        echo "[prepare] cache hit (sha256 match): $dest"
        return 0
      fi
      echo "[prepare] cache present but sha256 mismatch; will re-download: $dest"
    else
      local current
      current="$(sha256_of "$dest" 2>/dev/null || true)"
      if [[ -n "$current" ]]; then
        echo "$current" >"$sha_sidecar"
        echo "[prepare] cache hit (sha256 recorded): $dest"
      else
        echo "[prepare] cache hit (sha256 unavailable): $dest"
      fi
      return 0
    fi
  fi

  mkdir -p "$(dirname "$dest")"
  echo "[prepare] downloading: $url -> $dest"
  set +e
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --retry 3 --retry-delay 2 -o "$tmp" "$url"
    rc=$?
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$tmp" "$url"
    rc=$?
  else
    rc=127
  fi
  set -e

  if [[ "$rc" -ne 0 ]]; then
    rm -f "$tmp" || true
    echo "[prepare] download failed (rc=$rc): $url" >&2
    return "$rc"
  fi
  mv "$tmp" "$dest"
  local current
  current="$(sha256_of "$dest" 2>/dev/null || true)"
  if [[ -n "$current" ]]; then
    echo "$current" >"$sha_sidecar"
  fi
  return 0
}

sha256_of() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  else
    local hash_py=""
    if [[ -n "${python_bin:-}" ]]; then
      hash_py="$python_bin"
    elif command -v python3 >/dev/null 2>&1; then
      hash_py="python3"
    else
      hash_py="python"
    fi
    "$hash_py" - <<PY
import hashlib, pathlib
p=pathlib.Path(${path@Q})
h=hashlib.sha256()
with p.open("rb") as f:
  for chunk in iter(lambda: f.read(1024*1024), b""):
    h.update(chunk)
print(h.hexdigest())
PY
  fi
}

warnings=()
dataset_sha256=""
model_sha256=""

tokenizer_cache="$cache_root/model/tokenizer.model"
tokenizer_path="$model_root/tokenizer.model"

alpaca_cache="$cache_root/dataset/alpaca_data.json"
alpaca_sample_cache="$cache_root/dataset/alpaca_data.sample${max_examples}.json"
split_ratio="0.1"

tokenized_dataset_dir="$dataset_root/alpaca_tokenized"
raw_dataset_dir="$dataset_root/alpaca_raw"
mkdir -p "$raw_dataset_dir"

if [[ -n "$python_bin" ]]; then
  # Download tokenizer.model
  if ! download_to "$model_source" "$tokenizer_cache"; then
    if [[ -s "$tokenizer_cache" ]]; then
      warnings+=("model download failed but cache exists; using cached tokenizer.model")
    else
      failure_category="download_failed"
      stage_status="failure"
      stage_exit_code=1
    fi
  fi

  # Download Alpaca dataset JSON
  if ! download_to "$dataset_source" "$alpaca_cache"; then
    if [[ -s "$alpaca_cache" ]]; then
      warnings+=("dataset download failed but cache exists; using cached alpaca_data.json")
    else
      failure_category="download_failed"
      stage_status="failure"
      stage_exit_code=1
    fi
  fi
fi

if [[ "$stage_exit_code" -eq 0 || "$stage_exit_code" -eq 1 ]]; then
  :
fi

if [[ "$stage_exit_code" -eq 1 && "$failure_category" == "download_failed" ]]; then
  echo "[prepare] cannot proceed without required downloads and no cache present" >&2
else
  # Copy cache -> assets directories
  if [[ -s "$tokenizer_cache" ]]; then
    cp -f "$tokenizer_cache" "$tokenizer_path"
  fi
  if [[ -s "$alpaca_cache" ]]; then
    cp -f "$alpaca_cache" "$raw_dataset_dir/alpaca_data.json"
  fi

  # Subsample deterministically
  if [[ -n "$python_bin" && -s "$alpaca_cache" ]]; then
    echo "[prepare] creating deterministic sample (${max_examples} examples): $alpaca_sample_cache"
    set +e
    "$python_bin" - <<PY
import json, pathlib
src = pathlib.Path(${alpaca_cache@Q})
dst = pathlib.Path(${alpaca_sample_cache@Q})
max_n = int(${max_examples@Q})
data = json.loads(src.read_text(encoding="utf-8"))
if not isinstance(data, list):
    raise SystemExit("alpaca_data.json expected a JSON array")
dst.write_text(json.dumps(data[:max_n], ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY
    rc=$?
    set -e
    if [[ "$rc" -ne 0 ]]; then
      failure_category="data"
      stage_status="failure"
      stage_exit_code=1
      echo "[prepare] failed to create dataset sample" >&2
    fi
  fi

  # Tokenize into InternEvo format
  if [[ -n "$python_bin" && -s "$alpaca_sample_cache" && -s "$tokenizer_path" ]]; then
    dataset_sha256="$(sha256_of "$alpaca_sample_cache" 2>/dev/null || true)"
    model_sha256="$(sha256_of "$tokenizer_path" 2>/dev/null || true)"

    manifest_path="$tokenized_dataset_dir/manifest.json"
    tokenize_needed=1
    if [[ -s "$manifest_path" \
      && -s "$tokenized_dataset_dir/train/en/dataset.bin" \
      && -s "$tokenized_dataset_dir/train/en/dataset.bin.meta" \
      && -s "$tokenized_dataset_dir/valid/en/dataset.bin" \
      && -s "$tokenized_dataset_dir/valid/en/dataset.bin.meta" ]]; then
      match="$(
        "$python_bin" - <<PY 2>/dev/null || true
import json, pathlib
m = pathlib.Path(${manifest_path@Q})
try:
  obj = json.loads(m.read_text(encoding="utf-8"))
except Exception:
  raise SystemExit(0)
ok = True
ok = ok and str(obj.get("sample_sha256","")) == str(${dataset_sha256@Q})
ok = ok and str(obj.get("tokenizer_sha256","")) == str(${model_sha256@Q})
ok = ok and str(obj.get("split_ratio","")) == str(${split_ratio@Q})
try:
  ok = ok and int(obj.get("max_examples", -1)) == int(${max_examples@Q})
except Exception:
  ok = False
print("1" if ok else "0")
PY
      )"
      if [[ "${match:-0}" == "1" ]]; then
        tokenize_needed=0
        echo "[prepare] tokenized dataset cache hit (manifest matches): $tokenized_dataset_dir"
      fi
    fi

    if [[ "$tokenize_needed" -eq 1 ]]; then
      echo "[prepare] tokenizing sample dataset -> $tokenized_dataset_dir (split_ratio=$split_ratio)"
      rm -rf "$tokenized_dataset_dir" || true
      mkdir -p "$tokenized_dataset_dir"
      set +e
      "$python_bin" tools/alpaca_tokenizer.py "$alpaca_sample_cache" "$tokenized_dataset_dir" "$tokenizer_path" --split_ratio "$split_ratio"
      rc=$?
      set -e
      if [[ "$rc" -ne 0 ]]; then
        if file_has_re "No module named|ModuleNotFoundError" "$log_txt"; then
          failure_category="deps"
        else
          failure_category="data"
        fi
        stage_status="failure"
        stage_exit_code=1
        echo "[prepare] tokenization failed (rc=$rc)" >&2
      else
        "$python_bin" - <<PY
import json, pathlib
from datetime import datetime, timezone
manifest = {
  "sample_path": str(${alpaca_sample_cache@Q}),
  "sample_sha256": str(${dataset_sha256@Q}),
  "tokenizer_path": str(${tokenizer_path@Q}),
  "tokenizer_sha256": str(${model_sha256@Q}),
  "max_examples": int(${max_examples@Q}),
  "split_ratio": str(${split_ratio@Q}),
  "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
pathlib.Path(${manifest_path@Q}).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\\n", encoding="utf-8")
PY
      fi
    fi
  else
    if [[ -z "$python_bin" ]]; then
      failure_category="missing_report"
      stage_status="failure"
      stage_exit_code=1
    elif [[ ! -s "$alpaca_sample_cache" ]]; then
      failure_category="data"
      stage_status="failure"
      stage_exit_code=1
    elif [[ ! -s "$tokenizer_path" ]]; then
      failure_category="model"
      stage_status="failure"
      stage_exit_code=1
    fi
  fi

  # Verify expected artifacts
  if [[ "$stage_exit_code" -ne 1 ]]; then
    if [[ ! -s "$tokenizer_path" ]]; then
      failure_category="model"
      stage_status="failure"
      stage_exit_code=1
      echo "[prepare] tokenizer.model not found after download/copy: $tokenizer_path" >&2
    fi
    if [[ ! -s "$tokenized_dataset_dir/train/en/dataset.bin" || ! -s "$tokenized_dataset_dir/train/en/dataset.bin.meta" ]]; then
      failure_category="data"
      stage_status="failure"
      stage_exit_code=1
      echo "[prepare] tokenized train dataset missing under: $tokenized_dataset_dir/train/en/" >&2
    fi
    if [[ ! -s "$tokenized_dataset_dir/valid/en/dataset.bin" || ! -s "$tokenized_dataset_dir/valid/en/dataset.bin.meta" ]]; then
      failure_category="data"
      stage_status="failure"
      stage_exit_code=1
      echo "[prepare] tokenized valid dataset missing under: $tokenized_dataset_dir/valid/en/" >&2
    fi
  fi

  if [[ "$stage_exit_code" -ne 1 ]]; then
    stage_status="success"
    stage_exit_code=0
    failure_category="not_applicable"
  fi
fi

git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
python_ver=""
if [[ -n "$python_bin" ]]; then
  python_ver="$("$python_bin" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
fi

if [[ -z "$dataset_sha256" && -s "$alpaca_sample_cache" ]]; then
  dataset_sha256="$(sha256_of "$alpaca_sample_cache" 2>/dev/null || true)"
fi
if [[ -z "$model_sha256" && -s "$tokenizer_path" ]]; then
  model_sha256="$(sha256_of "$tokenizer_path" 2>/dev/null || true)"
fi

error_excerpt="$(tail -n 200 "$log_txt" || true)"

export WARNINGS_TXT=""
if [[ "${#warnings[@]}" -gt 0 ]]; then
  WARNINGS_TXT="$(printf '%s\n' "${warnings[@]}")"
fi

export PREPARE_STATUS="$stage_status"
export PREPARE_EXIT_CODE="$stage_exit_code"
export PREPARE_SKIP_REASON="$skip_reason"
export PREPARE_COMMAND="$command_str"
export PREPARE_DATASET_PATH="$tokenized_dataset_dir"
export PREPARE_DATASET_SOURCE="$dataset_source"
export PREPARE_DATASET_VERSION="$dataset_version"
export PREPARE_DATASET_SHA256="$dataset_sha256"
export PREPARE_MODEL_PATH="$tokenizer_path"
export PREPARE_MODEL_SOURCE="$model_source"
export PREPARE_MODEL_VERSION="$model_version"
export PREPARE_MODEL_SHA256="$model_sha256"
export PREPARE_PYTHON_BIN="$python_bin"
export PREPARE_PYTHON_VER="$python_ver"
export PREPARE_GIT_COMMIT="$git_commit"
export PREPARE_DECISION_REASON="$decision_reason"
export PREPARE_FAILURE_CATEGORY="$failure_category"
export PREPARE_ERROR_EXCERPT="$error_excerpt"
export PREPARE_MAX_EXAMPLES="$max_examples"
export PREPARE_RESULTS_JSON="$results_json"

python - <<'PY'
import json, os
warnings = os.environ.get("WARNINGS_TXT", "").splitlines()
out = {
  "status": os.environ.get("PREPARE_STATUS","failure"),
  "skip_reason": os.environ.get("PREPARE_SKIP_REASON","unknown"),
  "exit_code": int(os.environ.get("PREPARE_EXIT_CODE","1") or "1"),
  "stage": "prepare",
  "task": "download",
  "command": os.environ.get("PREPARE_COMMAND",""),
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {
      "path": os.environ.get("PREPARE_DATASET_PATH",""),
      "source": os.environ.get("PREPARE_DATASET_SOURCE",""),
      "version": os.environ.get("PREPARE_DATASET_VERSION",""),
      "sha256": os.environ.get("PREPARE_DATASET_SHA256",""),
    },
    "model": {
      "path": os.environ.get("PREPARE_MODEL_PATH",""),
      "source": os.environ.get("PREPARE_MODEL_SOURCE",""),
      "version": os.environ.get("PREPARE_MODEL_VERSION",""),
      "sha256": os.environ.get("PREPARE_MODEL_SHA256",""),
    },
  },
  "meta": {
    "python": (os.environ.get("PREPARE_PYTHON_BIN","") + (" (" + os.environ.get("PREPARE_PYTHON_VER","") + ")" if os.environ.get("PREPARE_PYTHON_VER","") else "")) if os.environ.get("PREPARE_PYTHON_BIN","") else "",
    "git_commit": os.environ.get("PREPARE_GIT_COMMIT",""),
    "env_vars": {k: os.environ.get(k, "") for k in [
      "PIP_CACHE_DIR","HF_HOME","HF_DATASETS_CACHE","TRANSFORMERS_CACHE","TORCH_HOME","SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON"
    ] if os.environ.get(k) is not None},
    "decision_reason": os.environ.get("PREPARE_DECISION_REASON",""),
    "warnings": warnings,
    "dataset": {"max_examples": int(os.environ.get("PREPARE_MAX_EXAMPLES","0") or "0")},
  },
  "failure_category": os.environ.get("PREPARE_FAILURE_CATEGORY","unknown"),
  "error_excerpt": os.environ.get("PREPARE_ERROR_EXCERPT",""),
}
open(os.environ.get("PREPARE_RESULTS_JSON","build_output/prepare/results.json"), "w", encoding="utf-8").write(
    json.dumps(out, indent=2, ensure_ascii=False) + "\n"
)
PY

exit "$stage_exit_code"
