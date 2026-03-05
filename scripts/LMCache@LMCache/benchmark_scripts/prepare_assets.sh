#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

stage_dir="build_output/prepare"
mkdir -p "$stage_dir"

log_txt="$stage_dir/log.txt"
results_json="$stage_dir/results.json"

assets_root="benchmark_assets"
cache_dir="$assets_root/cache"
dataset_dir="$assets_root/dataset"
model_dir="$assets_root/model"
manifest_path="$assets_root/manifest.json"

mkdir -p "$cache_dir" "$dataset_dir" "$model_dir"

exec >"$log_txt" 2>&1

echo "[prepare] repo_root=$repo_root"

status="failure"
skip_reason="unknown"
exit_code=1
failure_category="unknown"
command_str="bash benchmark_scripts/prepare_assets.sh"

dataset_url_default="https://raw.githubusercontent.com/YaoJiayi/CacheBlend/main/inputs/musique_s.json"
dataset_url="${SCIMLOPSBENCH_DATASET_URL:-$dataset_url_default}"
dataset_version="${SCIMLOPSBENCH_DATASET_VERSION:-main}"
dataset_cache_path="$cache_dir/dataset/musique_s.json"
dataset_out_path="$dataset_dir/musique_s.json"

model_id="${SCIMLOPSBENCH_MODEL_ID:-sshleifer/tiny-gpt2}"
model_revision="${SCIMLOPSBENCH_MODEL_REVISION:-main}"
model_id_sanitized="${model_id//\//__}"
model_cache_root="$cache_dir/model/$model_id_sanitized"
model_out_root="$model_dir/$model_id_sanitized"

lmcache_config_path="$model_dir/lmcache_standalone_config.yaml"

download_file() {
  local url="$1"
  local dest="$2"
  local tmp="${dest}.part"
  mkdir -p "$(dirname "$dest")"

  if [[ -s "$dest" ]]; then
    echo "[prepare] cache hit: $dest"
    return 0
  fi

  echo "[prepare] downloading: $url"
  if command -v curl >/dev/null 2>&1; then
    if curl -L --fail --retry 3 --retry-delay 2 -o "$tmp" "$url"; then
      mv -f "$tmp" "$dest"
      return 0
    fi
  elif command -v wget >/dev/null 2>&1; then
    if wget -O "$tmp" "$url"; then
      mv -f "$tmp" "$dest"
      return 0
    fi
  else
    echo "[prepare] ERROR: neither curl nor wget is available"
    return 1
  fi

  rm -f "$tmp" || true
  if [[ -s "$dest" ]]; then
    echo "[prepare] WARNING: download failed but cache exists: $dest"
    return 0
  fi
  echo "[prepare] ERROR: download failed and cache missing: $url"
  return 1
}

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  else
    python3 - <<PY
import hashlib, pathlib
p=pathlib.Path("$path")
h=hashlib.sha256()
h.update(p.read_bytes())
print(h.hexdigest())
PY
  fi
}

sha256_dir() {
  local dir="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    # Hash of (relative_path, file_sha256) pairs for determinism.
    (
      cd "$dir"
      find . -type f -print0 | sort -z | xargs -0 sha256sum
    ) | sha256sum | awk '{print $1}'
  else
    python3 - <<PY
import hashlib, os
root="$dir"
pairs=[]
for dp, _, files in os.walk(root):
  for fn in files:
    path=os.path.join(dp, fn)
    rel=os.path.relpath(path, root)
    h=hashlib.sha256()
    with open(path,"rb") as f:
      for chunk in iter(lambda: f.read(1024*1024), b""):
        h.update(chunk)
    pairs.append((rel, h.hexdigest()))
pairs.sort()
h=hashlib.sha256()
for rel, digest in pairs:
  h.update(rel.encode("utf-8", "replace") + b"\\0" + digest.encode("ascii") + b"\\n")
print(h.hexdigest())
PY
  fi
}

echo "[prepare] dataset_url=$dataset_url"
echo "[prepare] model_id=$model_id revision=$model_revision"

dataset_ok=0
model_ok=0

if download_file "$dataset_url" "$dataset_cache_path"; then
  mkdir -p "$(dirname "$dataset_out_path")"
  cp -f "$dataset_cache_path" "$dataset_out_path"
  dataset_ok=1
else
  failure_category="download_failed"
fi

model_files=(
  "config.json"
  "pytorch_model.bin"
  "tokenizer.json"
  "tokenizer_config.json"
  "vocab.json"
  "merges.txt"
  "special_tokens_map.json"
  "generation_config.json"
)

mkdir -p "$model_cache_root"
for f in "${model_files[@]}"; do
  url="https://huggingface.co/${model_id}/resolve/${model_revision}/${f}"
  if ! download_file "$url" "$model_cache_root/$f"; then
    echo "[prepare] WARNING: failed to download model file: $f"
  fi
done

if [[ -s "$model_cache_root/config.json" ]] && [[ -s "$model_cache_root/pytorch_model.bin" ]]; then
  rm -rf "$model_out_root" || true
  mkdir -p "$model_out_root"
  cp -a "$model_cache_root/." "$model_out_root/"
  model_ok=1
else
  echo "[prepare] ERROR: required model files missing in cache (need config.json and pytorch_model.bin)"
  failure_category="download_failed"
fi

# Minimal LMCache standalone config used by CPU/GPU stages.
cat >"$lmcache_config_path" <<'YAML'
chunk_size: 16
local_cpu: true
max_local_cpu_size: 0.1
internal_api_server_enabled: false
YAML

dataset_sha=""
model_sha=""
if [[ $dataset_ok -eq 1 ]]; then
  dataset_sha="$(sha256_file "$dataset_out_path")"
fi
if [[ $model_ok -eq 1 ]]; then
  model_sha="$(sha256_dir "$model_out_root")"
fi

python3 - <<PY
import json
from pathlib import Path

manifest = {
  "dataset": {
    "path": "$dataset_out_path" if $dataset_ok else "",
    "source": "$dataset_url",
    "version": "$dataset_version",
    "sha256": "$dataset_sha",
  },
  "model": {
    "path": "$model_out_root" if $model_ok else "",
    "source": "huggingface:$model_id",
    "version": "$model_revision",
    "sha256": "$model_sha",
  },
  "lmcache_config": {
    "path": "$lmcache_config_path",
  },
}
Path("$manifest_path").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
print("[prepare] wrote manifest:", "$manifest_path")
PY

if [[ $dataset_ok -eq 1 ]] && [[ $model_ok -eq 1 ]]; then
  status="success"
  skip_reason="not_applicable"
  exit_code=0
  failure_category="unknown"
else
  status="failure"
  skip_reason="unknown"
  exit_code=1
fi

ERROR_EXCERPT="$(tail -n 220 "$log_txt" 2>/dev/null || true)"
export ERROR_EXCERPT

python3 - <<PY
import json
import os
from pathlib import Path

payload = {
  "status": "$status",
  "skip_reason": "$skip_reason",
  "exit_code": int("$exit_code"),
  "stage": "prepare",
  "task": "download",
  "command": "$command_str",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "$dataset_out_path" if $dataset_ok else "", "source": "$dataset_url", "version": "$dataset_version", "sha256": "$dataset_sha"},
    "model": {"path": "$model_out_root" if $model_ok else "", "source": "huggingface:$model_id", "version": "$model_revision", "sha256": "$model_sha"},
  },
  "meta": {
    "python": "python3",
    "git_commit": "",
    "env_vars": {
      "SCIMLOPSBENCH_DATASET_URL": "$dataset_url",
      "SCIMLOPSBENCH_MODEL_ID": "$model_id",
      "SCIMLOPSBENCH_MODEL_REVISION": "$model_revision",
    },
    "decision_reason": "Downloads a small public dataset and a tiny HuggingFace model (config + weights) into benchmark_assets/, with offline cache reuse.",
  },
  "failure_category": "$failure_category",
  "error_excerpt": os.environ.get("ERROR_EXCERPT", ""),
}
Path("$results_json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY

echo "[prepare] done status=$status exit_code=$exit_code"
exit "$exit_code"
