#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (minimal dataset + minimal model artifact) for the repo entrypoints.

Writes:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Creates (if missing):
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Options:
  --python <path>          Python executable to use for asset preparation (recommended).
  --report-path <path>     Override report.json path (default: /opt/scimlopsbench/report.json or $SCIMLOPSBENCH_REPORT).
  --out-root <path>        Default: build_output
  --assets-root <path>     Default: benchmark_assets
EOF
}

python_bin=""
report_path=""
out_root="build_output"
assets_root="benchmark_assets"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --out-root) out_root="${2:-}"; shift 2 ;;
    --assets-root) assets_root="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/$out_root/prepare"
assets_cache="$repo_root/$assets_root/cache"
assets_dataset="$repo_root/$assets_root/dataset"
assets_model="$repo_root/$assets_root/model"

mkdir -p "$stage_dir" "$assets_cache" "$assets_dataset" "$assets_model"

log_txt="$stage_dir/log.txt"
results_json="$stage_dir/results.json"

exec > >(tee "$log_txt") 2>&1

command_str="bash benchmark_scripts/prepare_assets.sh"
[[ -n "$python_bin" ]] && command_str+=" --python $(printf '%q' "$python_bin")"
[[ -n "$report_path" ]] && command_str+=" --report-path $(printf '%q' "$report_path")"
command_str+=" --out-root $(printf '%q' "$out_root") --assets-root $(printf '%q' "$assets_root")"

echo "[prepare] repo_root=$repo_root"
echo "[prepare] stage_dir=$stage_dir"
echo "[prepare] assets_root=$repo_root/$assets_root"

report_path="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"

resolve_python() {
  if [[ -n "${python_bin:-}" ]]; then
    echo "$python_bin"
    return 0
  fi
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    echo "$SCIMLOPSBENCH_PYTHON"
    return 0
  fi
  if [[ -f "$report_path" ]]; then
    python3 - <<PY 2>/dev/null || true
import json
from pathlib import Path
p = Path(${report_path@Q})
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print(data.get("python_path",""))
except Exception:
    print("")
PY
    return 0
  fi
  echo ""
}

py="$(resolve_python)"

status="failure"
exit_code=1
failure_category="unknown"
skip_reason="unknown"
error_excerpt=""

dataset_source="synthetic_parquet"
model_source="repo:lcm/cards/mock_data/dummy_normalizer.pt"
decision_reason="Create a tiny, fully local parquet dataset with SONAR-like embeddings for lcm.train, and reuse the repo-shipped dummy sonar normalizer checkpoint as a minimal model artifact."

git_commit="$(cd "$repo_root" && git rev-parse HEAD 2>/dev/null || true)"
py_version=""

write_results_json() {
  RESULTS_JSON_PATH="$results_json" \
    LOG_PATH="$log_txt" \
    STATUS="$status" \
    EXIT_CODE="$exit_code" \
    STAGE="prepare" \
    TASK="download" \
    COMMAND_STR="$command_str" \
    TIMEOUT_SEC="1200" \
    FRAMEWORK="pytorch" \
    DATASET_PATH="$dataset_dir" \
    DATASET_SOURCE="$dataset_source" \
    DATASET_VERSION="$git_commit" \
    DATASET_SHA256="$dataset_sha" \
    MODEL_PATH="$model_file_dst" \
    MODEL_SOURCE="$model_source" \
    MODEL_VERSION="$git_commit" \
    MODEL_SHA256="$model_sha" \
    PYTHON_BIN="$py" \
    PYTHON_VERSION="$py_version" \
    GIT_COMMIT="$git_commit" \
    FAILURE_CATEGORY="$failure_category" \
    DECISION_REASON="$decision_reason" \
    python3 - <<'PY'
import json
import os
from pathlib import Path

def tail_file(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:]) if len(lines) > max_lines else "\n".join(lines)

results_path = Path(os.environ["RESULTS_JSON_PATH"])
log_path = Path(os.environ.get("LOG_PATH", ""))

payload = {
    "status": os.environ.get("STATUS", "failure"),
    "skip_reason": "unknown",
    "exit_code": int(os.environ.get("EXIT_CODE", "1") or 1),
    "stage": os.environ.get("STAGE", "prepare"),
    "task": os.environ.get("TASK", "download"),
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "1200") or 1200),
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
        "python": os.environ.get("PYTHON_BIN", ""),
        "python_version": os.environ.get("PYTHON_VERSION", ""),
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        },
        "decision_reason": os.environ.get("DECISION_REASON", ""),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail_file(log_path),
}

results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

dataset_dir="$assets_dataset/lcm_toy_pretraining"
model_file_src="$repo_root/lcm/cards/mock_data/dummy_normalizer.pt"
model_file_dst="$assets_model/dummy_sonar_normalizer.pt"

dataset_sha=""
model_sha=""

compute_sha() {
  "$py" - <<PY
import hashlib
import os
from pathlib import Path

target = Path(${1@Q})

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def sha256_dir(path: Path) -> str:
    files = []
    for root, _, filenames in os.walk(path):
        for fn in filenames:
            p = Path(root) / fn
            if p.is_symlink() or p.is_file():
                files.append(p)
    files = sorted(files, key=lambda p: p.as_posix())
    h = hashlib.sha256()
    for p in files:
        rel = p.relative_to(path).as_posix().encode("utf-8")
        h.update(rel + b"\n")
        if p.is_symlink():
            h.update(os.readlink(p).encode("utf-8") + b"\n")
        else:
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
    return h.hexdigest()

if target.is_dir():
    print(sha256_dir(target))
else:
    print(sha256_file(target))
PY
}

if [[ -z "${py:-}" ]]; then
  echo "[prepare] ERROR: Could not resolve python (report missing/invalid and --python not provided)."
  failure_category="missing_report"
  decision_reason="Failed to resolve python from report.json for asset preparation."
  status="failure"
  exit_code=1
  write_results_json
  exit 1
fi

echo "[prepare] using python: $py"

python_ok=0
if "$py" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  python_ok=1
fi

if [[ "$python_ok" -ne 1 ]]; then
  echo "[prepare] ERROR: python is not runnable: $py"
  failure_category="path_hallucination"
  decision_reason="Resolved python could not be executed."
  status="failure"
  exit_code=1
  write_results_json
  exit 1
fi

reused=0
if [[ -d "$dataset_dir" && -f "$model_file_dst" && -f "$results_json" ]]; then
  echo "[prepare] Existing assets detected; checking sha256 for reuse."
  set +e
  dataset_sha="$(compute_sha "$dataset_dir" 2>/dev/null)"
  model_sha="$(compute_sha "$model_file_dst" 2>/dev/null)"
  set -e
  if [[ -n "$dataset_sha" && -n "$model_sha" ]]; then
    prev_dataset_sha="$("$py" - <<PY 2>/dev/null || true
import json
from pathlib import Path
p = Path(${results_json@Q})
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("assets",{}).get("dataset",{}).get("sha256",""))
except Exception:
    print("")
PY
)"
    prev_model_sha="$("$py" - <<PY 2>/dev/null || true
import json
from pathlib import Path
p = Path(${results_json@Q})
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("assets",{}).get("model",{}).get("sha256",""))
except Exception:
    print("")
PY
)"
    if [[ "$dataset_sha" == "$prev_dataset_sha" && "$model_sha" == "$prev_model_sha" ]]; then
      echo "[prepare] Reusing cached assets (sha256 match)."
      reused=1
    fi
  fi
fi

if [[ "$reused" -ne 1 ]]; then
  echo "[prepare] Generating minimal synthetic parquet dataset and minimal model artifact."

  cache_run_dir="$assets_cache/prepare_run"
  cache_dataset_dir="$cache_run_dir/dataset_tmp"
  cache_model_dir="$cache_run_dir/model_tmp"
  rm -rf "$cache_run_dir"
  mkdir -p "$cache_dataset_dir" "$cache_model_dir"

  set +e
  CACHE_DATASET_DIR="$cache_dataset_dir" "$py" - <<'PY'
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import torch
from stopes.utils.arrow_utils import nested_numpy_to_pyarrow

root = Path(os.environ["CACHE_DATASET_DIR"])
root.mkdir(parents=True, exist_ok=True)

def make_rows(num_rows: int, split: str):
    sonar_dim = 1024
    sonar_std = 0.006
    seq_len = 4
    texts = [
        [f"Hello from {split} row {i} sent {j}." for j in range(seq_len)]
        for i in range(num_rows)
    ]
    embs = torch.randn(size=[num_rows, seq_len, sonar_dim]) * sonar_std
    embs_pa = nested_numpy_to_pyarrow([row.numpy() for row in embs])
    table = pa.Table.from_pydict(
        {
            "split": [split] * num_rows,
            "text_sentences": texts,
            "text_sentences_sonar_emb": embs_pa,
        }
    )
    return table

train = make_rows(num_rows=8, split="train")
valid = make_rows(num_rows=4, split="validation")
all_data = pa.concat_tables([train, valid])

pq.write_to_dataset(
    all_data,
    root,
    partition_cols=["split"],
    row_group_size=1,
)
print(f"Wrote parquet dataset to {root}")
PY
  gen_rc=$?
  set -e

  if [[ "$gen_rc" -ne 0 ]]; then
    echo "[prepare] ERROR: dataset generation failed (rc=$gen_rc)."
    failure_category="deps"
    decision_reason="Generate a tiny parquet dataset compatible with lcm.train using random SONAR-like embeddings."
    status="failure"
    exit_code=1
    write_results_json
    exit 1
  fi

  if [[ ! -f "$model_file_src" ]]; then
    echo "[prepare] ERROR: expected model artifact not found: $model_file_src"
    failure_category="model"
    decision_reason="Use repo-provided dummy sonar normalizer weights as a minimal model artifact."
    status="failure"
    exit_code=1
    write_results_json
    exit 1
  fi

  cp -f "$model_file_src" "$cache_model_dir/dummy_sonar_normalizer.pt"
  cp -f "$repo_root/lcm/cards/sonar_normalizer.yaml" "$cache_model_dir/sonar_normalizer.yaml" || true

  rm -rf "$dataset_dir"
  mkdir -p "$dataset_dir"
  cp -a "$cache_dataset_dir/." "$dataset_dir/"

  cp -f "$cache_model_dir/dummy_sonar_normalizer.pt" "$model_file_dst"
  cp -f "$cache_model_dir/sonar_normalizer.yaml" "$assets_model/sonar_normalizer.yaml" || true

  dataset_sha="$(compute_sha "$dataset_dir")"
  model_sha="$(compute_sha "$model_file_dst")"
fi

if [[ -z "$dataset_sha" ]]; then
  dataset_sha="$(compute_sha "$dataset_dir" 2>/dev/null || true)"
fi
if [[ -z "$model_sha" ]]; then
  model_sha="$(compute_sha "$model_file_dst" 2>/dev/null || true)"
fi

if [[ ! -d "$dataset_dir" || -z "$dataset_sha" ]]; then
  echo "[prepare] ERROR: dataset directory missing or sha256 unavailable: $dataset_dir"
  failure_category="data"
  error_excerpt="$(tail -n 220 "$log_txt" || true)"
  status="failure"
  exit_code=1
else
  status="success"
  exit_code=0
fi

if [[ ! -f "$model_file_dst" || -z "$model_sha" ]]; then
  echo "[prepare] ERROR: model file missing or sha256 unavailable: $model_file_dst"
  failure_category="model"
  status="failure"
  exit_code=1
fi

git_commit="$(cd "$repo_root" && git rev-parse HEAD 2>/dev/null || true)"
py_version="$("$py" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"

write_results_json

exit "$exit_code"
