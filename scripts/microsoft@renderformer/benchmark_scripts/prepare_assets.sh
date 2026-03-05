#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare minimal benchmark assets:
  - Generate minimal HDF5 dataset files from examples (no external download)
  - Download minimal pretrained model weights to benchmark_assets/cache and link into benchmark_assets/model

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Optional:
  --report-path <path>     Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --python <path>          Explicit python executable (highest priority)
  --model-id <repo_id>     Default: microsoft/renderformer-v1-base
EOF
}

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
stage="prepare"
task="download"
out_dir="$repo_root/build_output/$stage"
log_path="$out_dir/log.txt"
results_json="$out_dir/results.json"

report_path=""
python_bin=""
model_id="microsoft/renderformer-v1-base"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --python)
      python_bin="${2:-}"; shift 2 ;;
    --model-id)
      model_id="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

mkdir -p "$out_dir"
: >"$log_path"
exec > >(tee -a "$log_path") 2>&1

status="failure"
exit_code=1
failure_category="unknown"
skip_reason="unknown"
command="bash benchmark_scripts/prepare_assets.sh"
decision_reason="Use local example scenes (examples/*.json) as dataset; download minimal public HF model weights; keep caches under benchmark_assets/cache for offline reuse."

dataset_primary_path=""
dataset_primary_source=""
dataset_primary_version=""
dataset_primary_sha256=""

model_path=""
model_source="$model_id"
model_version=""
model_sha256=""
model_weight_file=""
model_link_path="$repo_root/benchmark_assets/model/renderformer-v1-base"

git_commit=""
if git -C "$repo_root" rev-parse HEAD >/dev/null 2>&1; then
  git_commit="$(git -C "$repo_root" rev-parse HEAD | tr -d '\n' || true)"
fi

if [[ -z "$report_path" ]]; then
  report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
fi

sha256_file() {
  local f="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$f" | awk '{print $1}'
    return 0
  fi
  python3 - <<PY
import hashlib, pathlib
p=pathlib.Path(${f@Q})
h=hashlib.sha256()
with p.open("rb") as f:
  for chunk in iter(lambda: f.read(1024*1024), b""):
    h.update(chunk)
print(h.hexdigest())
PY
}

write_results() {
  python3 - <<PY
import json, os, pathlib, subprocess

out_dir = pathlib.Path(${out_dir@Q})
log_path = out_dir / "log.txt"
repo_root = pathlib.Path(${repo_root@Q})

def tail(path: pathlib.Path, n: int = 220) -> str:
  try:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
  except Exception:
    return ""
  return "\n".join(lines[-n:])

meta_env_keys = [
  "SCIMLOPSBENCH_REPORT",
  "SCIMLOPSBENCH_PYTHON",
  "HF_HOME",
  "HF_HUB_CACHE",
  "TRANSFORMERS_CACHE",
  "TORCH_HOME",
  "PIP_CACHE_DIR",
  "IMAGEIO_USERDIR",
  "XDG_CACHE_HOME",
  "TMPDIR",
]
env_vars = {k: os.environ.get(k, "") for k in meta_env_keys if os.environ.get(k)}

git_commit = ${git_commit@Q}

dataset_files = []
try:
  dataset_files = json.loads(os.environ.get("PREP_DATASET_FILES_JSON", "[]") or "[]")
except Exception:
  dataset_files = []
if not dataset_files:
  root = repo_root / "benchmark_assets" / "dataset"
  if root.exists():
    dataset_files = sorted(str(p) for p in root.rglob("*.h5"))

payload = {
  "status": ${status@Q},
  "skip_reason": ${skip_reason@Q},
  "exit_code": int(${exit_code}),
  "stage": "prepare",
  "task": "download",
  "command": ${command@Q},
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": ${dataset_primary_path@Q}, "source": ${dataset_primary_source@Q}, "version": ${dataset_primary_version@Q}, "sha256": ${dataset_primary_sha256@Q}},
    "model": {"path": ${model_path@Q}, "source": ${model_source@Q}, "version": ${model_version@Q}, "sha256": ${model_sha256@Q}},
  },
  "meta": {
    "python": os.environ.get("SCIMLOPSBENCH_PYTHON", "") or ${python_bin@Q},
    "git_commit": git_commit,
    "env_vars": env_vars,
    "decision_reason": ${decision_reason@Q},
    "dataset_files": dataset_files,
    "model_weight_file": ${model_weight_file@Q},
    "model_link_path": ${model_link_path@Q},
  },
  "failure_category": ${failure_category@Q},
  "error_excerpt": tail(log_path),
}
(out_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

on_exit() {
  write_results || true
}
trap on_exit EXIT

cd "$repo_root"

if [[ -z "$python_bin" ]]; then
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    python_bin="${SCIMLOPSBENCH_PYTHON}"
  else
    if [[ ! -f "$report_path" ]]; then
      echo "[prepare] missing report: $report_path" >&2
      failure_category="missing_report"
      exit_code=1
      exit 1
    fi
    python_bin="$(python3 - <<PY 2>/dev/null || true
import json, sys
from pathlib import Path
p=Path(${report_path@Q})
try:
  d=json.loads(p.read_text(encoding="utf-8"))
  print(d.get("python_path","") or "")
except Exception:
  print("")
PY
    )"
    if [[ -z "$python_bin" ]]; then
      echo "[prepare] report missing python_path: $report_path" >&2
      failure_category="missing_report"
      exit_code=1
      exit 1
    fi
  fi
fi

if [[ ! -x "$python_bin" ]]; then
  echo "[prepare] python is not executable: $python_bin" >&2
  failure_category="path_hallucination"
  exit_code=1
  exit 1
fi

echo "[prepare] using python: $python_bin"

export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HOME="$repo_root/benchmark_assets/cache/huggingface"
export HF_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export PIP_CACHE_DIR="$repo_root/benchmark_assets/cache/pip"
export IMAGEIO_USERDIR="$repo_root/benchmark_assets/cache/imageio"
export TMPDIR="$repo_root/benchmark_assets/cache/tmp"

mkdir -p "$XDG_CACHE_HOME" "$HF_HUB_CACHE" "$TRANSFORMERS_CACHE" "$TORCH_HOME" "$PIP_CACHE_DIR" "$IMAGEIO_USERDIR" "$TMPDIR"
mkdir -p "$repo_root/benchmark_assets/model"

dataset_files=()
mesh_dir="$repo_root/benchmark_assets/cache/scene_meshes"
mkdir -p "$mesh_dir"

for scene in cbox shader-ball; do
  cfg="$repo_root/examples/${scene}.json"
  if [[ ! -f "$cfg" ]]; then
    echo "[prepare] missing example scene config: $cfg" >&2
    failure_category="data"
    exit_code=1
    exit 1
  fi
  scene_out_dir="$repo_root/benchmark_assets/dataset/$scene"
  mkdir -p "$scene_out_dir"
  h5_path="$scene_out_dir/${scene}.h5"
  mesh_path="$mesh_dir/${scene}.obj"
  if [[ -f "$h5_path" ]]; then
    echo "[prepare] dataset exists, reuse: $h5_path"
  else
    echo "[prepare] generating dataset: $h5_path"
    "$python_bin" scene_processor/convert_scene.py "$cfg" --mesh_path "$mesh_path" --output_h5_path "$h5_path"
  fi
  if [[ ! -f "$h5_path" ]]; then
    echo "[prepare] failed to generate dataset: $h5_path" >&2
    failure_category="data"
    exit_code=1
    exit 1
  fi
  dataset_files+=("$h5_path")
done

if [[ "${#dataset_files[@]}" -eq 0 ]]; then
  mapfile -t dataset_files < <(find "$repo_root/benchmark_assets/dataset" -type f -name "*.h5" 2>/dev/null | sort -u)
fi

export PREP_DATASET_FILES_JSON
PREP_DATASET_FILES_JSON="$(printf '%s\n' "${dataset_files[@]}" | python3 - <<'PY'
import json, sys
items=[ln.strip() for ln in sys.stdin if ln.strip()]
print(json.dumps(items))
PY
)"

dataset_primary_path="$repo_root/benchmark_assets/dataset/cbox/cbox.h5"
dataset_primary_source="repo://examples/cbox.json"
dataset_primary_version="${git_commit:-}"
dataset_primary_sha256="$(sha256_file "$dataset_primary_path")"

echo "[prepare] dataset primary: $dataset_primary_path (sha256=$dataset_primary_sha256)"

if [[ -L "$model_link_path" || -d "$model_link_path" ]]; then
  echo "[prepare] model link exists, reuse: $model_link_path"
  model_path="$(python3 - <<PY
import os, pathlib
p=pathlib.Path(${model_link_path@Q})
print(os.path.realpath(p))
PY
  )"
else
  echo "[prepare] downloading model: $model_id"
  set +e
  model_download_json="$(MODEL_ID="$model_id" "$python_bin" - <<'PY'
import hashlib
import json
import os
import pathlib
import sys

repo_id = os.environ.get("MODEL_ID", "").strip()
cache_dir = os.environ.get("HF_HOME", "").strip()

if not repo_id:
  print("[prepare:model] MODEL_ID is empty", file=sys.stderr)
  raise SystemExit(2)

try:
  from huggingface_hub import HfApi, snapshot_download
except Exception as e:
  print(f"[prepare:model] missing dependency huggingface_hub: {e}", file=sys.stderr)
  raise SystemExit(3)

model_sha = ""
try:
  model_sha = HfApi().model_info(repo_id).sha or ""
except Exception:
  model_sha = ""

def download(local_files_only: bool) -> str:
  return snapshot_download(
    repo_id=repo_id,
    revision=None,
    cache_dir=cache_dir or None,
    local_files_only=local_files_only,
  )

try:
  snapshot_dir = download(local_files_only=False)
except Exception as e:
  print(f"[prepare:model] online download failed: {e}", file=sys.stderr)
  snapshot_dir = download(local_files_only=True)

root = pathlib.Path(snapshot_dir)
if not root.exists():
  print(f"[prepare:model] snapshot dir does not exist: {snapshot_dir}", file=sys.stderr)
  raise SystemExit(4)

def pick_weight_file(r: pathlib.Path) -> pathlib.Path | None:
  candidates = []
  for p in r.rglob("*"):
    if not p.is_file():
      continue
    name = p.name
    if name == "model.safetensors" or name.endswith(".safetensors") or name == "pytorch_model.bin":
      try:
        candidates.append((p.stat().st_size, p))
      except Exception:
        continue
  if not candidates:
    return None
  candidates.sort(key=lambda x: x[0], reverse=True)
  return candidates[0][1]

def sha256(path: pathlib.Path) -> str:
  h = hashlib.sha256()
  with path.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
      h.update(chunk)
  return h.hexdigest()

wf = pick_weight_file(root)
payload = {
  "repo_id": repo_id,
  "model_sha": model_sha,
  "snapshot_dir": str(root),
  "weight_file": str(wf) if wf else "",
  "weight_sha256": sha256(wf) if wf else "",
}
print(json.dumps(payload))
PY
  )"
  dl_rc=$?
  set -e
  if [[ "$dl_rc" -ne 0 ]]; then
    echo "[prepare] model download failed (rc=$dl_rc)" >&2
    if [[ "$dl_rc" -eq 3 ]]; then
      failure_category="deps"
    else
      failure_category="download_failed"
    fi
    exit_code=1
    exit 1
  fi
  model_path="$(python3 - <<PY
import json
print(json.loads(${model_download_json@Q}).get("snapshot_dir",""))
PY
  )"
  model_version="$(python3 - <<PY
import json
print(json.loads(${model_download_json@Q}).get("model_sha","") or "")
PY
  )"
  model_weight_file="$(python3 - <<PY
import json
print(json.loads(${model_download_json@Q}).get("weight_file","") or "")
PY
  )"
  model_sha256="$(python3 - <<PY
import json
print(json.loads(${model_download_json@Q}).get("weight_sha256","") or "")
PY
  )"
  if [[ -z "$model_path" ]]; then
    echo "[prepare] model download did not return a local path" >&2
    failure_category="download_failed"
    exit_code=1
    exit 1
  fi
  if [[ ! -d "$model_path" ]]; then
    echo "[prepare] model download returned non-existent directory: $model_path" >&2
    failure_category="model"
    exit_code=1
    exit 1
  fi
  ln -sfn "$model_path" "$model_link_path"
fi

if [[ ! -d "$model_path" ]]; then
  echo "[prepare] resolved model directory does not exist: $model_path" >&2
  failure_category="model"
  exit_code=1
  exit 1
fi

if [[ -z "$model_weight_file" || -z "$model_sha256" ]]; then
  model_info_json="$(MODEL_DIR="$model_path" "$python_bin" - <<'PY'
import hashlib
import json
import os
import pathlib

model_dir = pathlib.Path(os.environ["MODEL_DIR"])

def pick_weight_file(root: pathlib.Path) -> pathlib.Path | None:
  candidates = []
  for p in root.rglob("*"):
    if not p.is_file():
      continue
    name = p.name
    if name == "model.safetensors" or name.endswith(".safetensors") or name == "pytorch_model.bin":
      try:
        candidates.append((p.stat().st_size, p))
      except Exception:
        continue
  if not candidates:
    return None
  candidates.sort(key=lambda x: x[0], reverse=True)
  return candidates[0][1]

def sha256(path: pathlib.Path) -> str:
  h = hashlib.sha256()
  with path.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
      h.update(chunk)
  return h.hexdigest()

wf = pick_weight_file(model_dir)
payload = {
  "model_dir": str(model_dir),
  "weight_file": str(wf) if wf else "",
  "weight_sha256": sha256(wf) if wf else "",
}
print(json.dumps(payload))
PY
  )"
  model_weight_file="$(python3 - <<PY
import json
print(json.loads(${model_info_json@Q}).get("weight_file","") or "")
PY
  )"
  model_sha256="$(python3 - <<PY
import json
print(json.loads(${model_info_json@Q}).get("weight_sha256","") or "")
PY
  )"
fi

if [[ -z "$model_version" ]]; then
  model_version="unknown"
fi

if [[ -z "$model_weight_file" || -z "$model_sha256" ]]; then
  echo "[prepare] could not locate a model weight file under: $model_path" >&2
  echo "[prepare] search root: $repo_root/benchmark_assets/cache" >&2
  failure_category="model"
  exit_code=1
  exit 1
fi

echo "[prepare] model dir: $model_path"
echo "[prepare] model weight: $model_weight_file (sha256=$model_sha256)"

echo "[prepare] exr smoketest via imageio"
exr_test_path="$out_dir/exr_smoketest.exr"
set +e
"$python_bin" - <<PY
import numpy as np
import imageio

img = (np.zeros((4, 4, 3), dtype=np.float32))
imageio.v3.imwrite(${exr_test_path@Q}, img)
print("exr_ok")
PY
exr_rc=$?
set -e
if [[ "$exr_rc" -ne 0 ]]; then
  echo "[prepare] exr write failed; attempting imageio freeimage download (may require internet)"
  set +e
  "$python_bin" - <<'PY'
import imageio
imageio.plugins.freeimage.download()
print("freeimage_download_ok")
PY
dl_rc=$?
set -e
if [[ "$dl_rc" -ne 0 ]]; then
    failure_category="download_failed"
    exit_code=1
    exit 1
  fi
  set +e
  "$python_bin" - <<PY
import numpy as np
import imageio
img = (np.zeros((4, 4, 3), dtype=np.float32))
imageio.v3.imwrite(${exr_test_path@Q}, img)
print("exr_ok_after_download")
PY
  exr_rc2=$?
  set -e
  if [[ "$exr_rc2" -ne 0 ]]; then
    failure_category="deps"
    exit_code=1
    exit 1
  fi
fi

status="success"
exit_code=0
failure_category="unknown"

exit 0
