#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model/weights) into benchmark_assets/.

Default behavior (this repo):
  - Uses bundled sample GeoJSON data from tests/data/ (no network required).
  - Creates a "model not applicable" placeholder, since the repo ships no weights.

Outputs (always written, even on failure):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Writes (only) into:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Optional:
  --repo <path>          Repo root (default: auto = parent of benchmark_scripts)
EOF
}

repo=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_DEFAULT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="${repo:-$REPO_ROOT_DEFAULT}"

STAGE_DIR="$REPO_ROOT/build_output/prepare"
LOG_PATH="$STAGE_DIR/log.txt"
RESULTS_JSON="$STAGE_DIR/results.json"

ASSETS_ROOT="$REPO_ROOT/benchmark_assets"
CACHE_DIR="$ASSETS_ROOT/cache"
DATASET_DIR="$ASSETS_ROOT/dataset"
MODEL_DIR="$ASSETS_ROOT/model"

mkdir -p "$STAGE_DIR" "$CACHE_DIR" "$DATASET_DIR" "$MODEL_DIR"
: >"$LOG_PATH"
exec > >(tee -a "$LOG_PATH") 2>&1

cd "$REPO_ROOT"

status="failure"
exit_code=1
failure_category="unknown"
skip_reason="unknown"
decision_reason="Prepared bundled sample dataset from tests/data; no external model weights referenced, so model asset is marked not_applicable."

git_commit="$(git rev-parse HEAD 2>/dev/null || true)"

sha256_file() {
  local f="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$f" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$f" | awk '{print $1}'
  else
    local py
    py="$(command -v python3 || command -v python || true)"
    if [[ -z "$py" ]]; then
      echo ""
      return 0
    fi
    "$py" - <<PY
import hashlib, pathlib
p = pathlib.Path("$f")
h = hashlib.sha256()
h.update(p.read_bytes())
print(h.hexdigest())
PY
  fi
}

DATASET_SRC_DIR="$REPO_ROOT/tests/data"
DATASET_CACHE_SUBDIR="$CACHE_DIR/city2graph_sample_geojson"
DATASET_OUT_SUBDIR="$DATASET_DIR/city2graph_sample_geojson"
DATASET_MANIFEST="$DATASET_CACHE_SUBDIR/manifest.json"

MODEL_CACHE_SUBDIR="$CACHE_DIR/model_not_applicable"
MODEL_OUT_SUBDIR="$MODEL_DIR/model_not_applicable"
MODEL_FILE="$MODEL_CACHE_SUBDIR/model.txt"

dataset_source="repo:tests/data"
dataset_version="${git_commit:-unknown}"
dataset_sha256=""
dataset_path="$DATASET_OUT_SUBDIR"

model_source="not_applicable"
model_version=""
model_sha256=""
model_path="$MODEL_OUT_SUBDIR"

echo "Preparing dataset..."
if [[ ! -f "$DATASET_SRC_DIR/sample_buildings.geojson" || ! -f "$DATASET_SRC_DIR/sample_segments.geojson" ]]; then
  echo "Required sample dataset files not found in tests/data/." >&2
  echo "Expected: tests/data/sample_buildings.geojson and tests/data/sample_segments.geojson" >&2
  failure_category="data"
else
  mkdir -p "$DATASET_CACHE_SUBDIR" "$DATASET_OUT_SUBDIR"

  # Copy into cache first (acts as the download cache).
  cp -f "$DATASET_SRC_DIR/sample_buildings.geojson" "$DATASET_CACHE_SUBDIR/sample_buildings.geojson"
  cp -f "$DATASET_SRC_DIR/sample_segments.geojson" "$DATASET_CACHE_SUBDIR/sample_segments.geojson"

  b_sha="$(sha256_file "$DATASET_CACHE_SUBDIR/sample_buildings.geojson")"
  s_sha="$(sha256_file "$DATASET_CACHE_SUBDIR/sample_segments.geojson")"

  # Stable manifest; sha256 of manifest is treated as dataset sha256.
  cat >"$DATASET_MANIFEST" <<JSON
{
  "name": "city2graph_sample_geojson",
  "source": "$dataset_source",
  "version": "$dataset_version",
  "files": [
    {"path": "sample_buildings.geojson", "sha256": "$b_sha"},
    {"path": "sample_segments.geojson", "sha256": "$s_sha"}
  ]
}
JSON

  dataset_sha256="$(sha256_file "$DATASET_MANIFEST")"

  # Copy into dataset dir (skip if sha256 matches on subsequent runs).
  existing_manifest_sha=""
  if [[ -f "$DATASET_OUT_SUBDIR/manifest.json" ]]; then
    existing_manifest_sha="$(sha256_file "$DATASET_OUT_SUBDIR/manifest.json")"
  fi
  if [[ -n "$existing_manifest_sha" && "$existing_manifest_sha" == "$dataset_sha256" ]]; then
    echo "Dataset already prepared (sha256 matches); skipping copy to dataset/."
  else
    cp -f "$DATASET_CACHE_SUBDIR/sample_buildings.geojson" "$DATASET_OUT_SUBDIR/sample_buildings.geojson"
    cp -f "$DATASET_CACHE_SUBDIR/sample_segments.geojson" "$DATASET_OUT_SUBDIR/sample_segments.geojson"
    cp -f "$DATASET_MANIFEST" "$DATASET_OUT_SUBDIR/manifest.json"
  fi

  echo "Dataset prepared at: $DATASET_OUT_SUBDIR"
  echo "Dataset manifest sha256: $dataset_sha256"
fi

echo "Preparing model asset..."
mkdir -p "$MODEL_CACHE_SUBDIR" "$MODEL_OUT_SUBDIR"
cat >"$MODEL_FILE" <<'TXT'
This repository (city2graph) is a GeoAI/graph-construction library and does not ship or require pretrained model weights/checkpoints.
This placeholder exists to satisfy the benchmark asset preparation contract.
TXT
model_sha256="$(sha256_file "$MODEL_FILE")"
existing_model_sha=""
if [[ -f "$MODEL_OUT_SUBDIR/model.txt" ]]; then
  existing_model_sha="$(sha256_file "$MODEL_OUT_SUBDIR/model.txt")"
fi
if [[ -n "$existing_model_sha" && "$existing_model_sha" == "$model_sha256" ]]; then
  echo "Model placeholder already prepared (sha256 matches); skipping copy to model/."
else
  cp -f "$MODEL_FILE" "$MODEL_OUT_SUBDIR/model.txt"
fi
echo "Model placeholder prepared at: $MODEL_OUT_SUBDIR"
echo "Model placeholder sha256: $model_sha256"

if [[ "$failure_category" == "unknown" ]]; then
  if [[ -z "$dataset_sha256" || ! -d "$DATASET_OUT_SUBDIR" ]]; then
    failure_category="data"
  else
    status="success"
    exit_code=0
  fi
fi

error_excerpt="$(tail -n 240 "$LOG_PATH" 2>/dev/null | tail -n 220 || true)"

SYS_PY="$(command -v python3 || command -v python || true)"
if [[ -z "$SYS_PY" ]]; then
  cat >"$RESULTS_JSON" <<JSON
{
  "status": "$status",
  "skip_reason": "$skip_reason",
  "exit_code": $exit_code,
  "stage": "prepare",
  "task": "download",
  "command": "prepare_assets.sh",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "$dataset_path", "source": "$dataset_source", "version": "$dataset_version", "sha256": "$dataset_sha256"},
    "model": {"path": "$model_path", "source": "$model_source", "version": "$model_version", "sha256": "$model_sha256"}
  },
  "meta": {
    "python": "",
    "git_commit": "$git_commit",
    "env_vars": {},
    "decision_reason": "$decision_reason"
  },
  "failure_category": "$failure_category",
  "error_excerpt": ""
}
JSON
else
  export status exit_code skip_reason failure_category error_excerpt decision_reason git_commit RESULTS_JSON
  export dataset_path dataset_source dataset_version dataset_sha256
  export model_path model_source model_version model_sha256
  "$SYS_PY" - <<'PY'
import json
import os

def env_snapshot() -> dict:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "PATH",
        "PYTHONPATH",
        "HF_AUTH_TOKEN",
        "HF_TOKEN",
    ]
    out = {}
    for k in keys:
        if k not in os.environ:
            continue
        v = os.environ.get(k, "")
        if any(s in k.upper() for s in ("TOKEN", "SECRET", "KEY", "PASS")) and v:
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out

payload = {
    "status": os.environ["status"],
    "skip_reason": os.environ.get("skip_reason", "unknown"),
    "exit_code": int(os.environ["exit_code"]),
    "stage": "prepare",
    "task": "download",
    "command": "prepare_assets.sh",
    "timeout_sec": 1200,
    "framework": "unknown",
    "assets": {
        "dataset": {
            "path": os.environ.get("dataset_path", ""),
            "source": os.environ.get("dataset_source", ""),
            "version": os.environ.get("dataset_version", ""),
            "sha256": os.environ.get("dataset_sha256", ""),
        },
        "model": {
            "path": os.environ.get("model_path", ""),
            "source": os.environ.get("model_source", ""),
            "version": os.environ.get("model_version", ""),
            "sha256": os.environ.get("model_sha256", ""),
        },
    },
    "meta": {
        "python": "",
        "git_commit": os.environ.get("git_commit", ""),
        "env_vars": env_snapshot(),
        "decision_reason": os.environ.get("decision_reason", ""),
    },
    "failure_category": os.environ.get("failure_category", "unknown"),
    "error_excerpt": os.environ.get("error_excerpt", "")[-8000:],
}

out_path = os.environ.get("RESULTS_JSON", "")
if not out_path:
    out_path = os.path.join(os.getcwd(), "build_output", "prepare", "results.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, ensure_ascii=False)
PY
fi

exit "$exit_code"
