#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model weights) for VoxCPM.

Default behavior (from repo docs/examples):
  - Dataset: copies examples/example.wav into benchmark_assets/dataset/ and writes a 1-line JSONL manifest.
  - Model: downloads HuggingFace model openbmb/VoxCPM-0.5B into benchmark_assets/cache/, then links into benchmark_assets/model/.

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Environment:
  SCIMLOPSBENCH_REPORT        Override report path (default: /opt/scimlopsbench/report.json)
  SCIMLOPSBENCH_PYTHON        Override python executable (highest priority for this script)
  VOXCPM_MODEL_ID             Override model id (default: openbmb/VoxCPM-0.5B)
  VOXCPM_MODEL_REVISION       Optional HF revision (e.g., a commit hash or tag)
  HF_TOKEN / HUGGINGFACE_HUB_TOKEN  Optional auth token for gated models
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root" || exit 1

stage="prepare"
task="download"
framework="pytorch"
timeout_sec=1200
out_dir="build_output/$stage"
log_file="$out_dir/log.txt"
results_json="$out_dir/results.json"

mkdir -p "$out_dir"
: >"$log_file"

export PYTHONDONTWRITEBYTECODE=1

PYHOST="$(command -v python3 || command -v python || true)"
if [[ -z "$PYHOST" ]]; then
  echo "[prepare] ERROR: python not found in PATH (required to parse report.json and write results.json)." >>"$log_file"
  cat >"$results_json" <<EOF
{"status":"failure","skip_reason":"not_applicable","exit_code":1,"stage":"prepare","task":"download","command":"","timeout_sec":1200,"framework":"unknown","assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},"meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":""},"failure_category":"deps","error_excerpt":"python not found"}
EOF
  exit 1
fi

git_commit=""
if command -v git >/dev/null 2>&1 && git rev-parse HEAD >/dev/null 2>&1; then
  git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
fi

report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
if [[ -n "$report_path" && -d "$report_path" ]]; then
  report_path="$report_path/report.json"
fi
export SCIMLOPSBENCH_REPORT="$report_path"
python_exe="${SCIMLOPSBENCH_PYTHON:-}"

model_id="${VOXCPM_MODEL_ID:-openbmb/VoxCPM-0.5B}"
model_revision="${VOXCPM_MODEL_REVISION:-}"
sample_rate="16000"
if [[ "$model_id" == *"VoxCPM1.5"* ]]; then
  sample_rate="44100"
fi

assets_root="benchmark_assets"
cache_root="$assets_root/cache"
dataset_dir="$assets_root/dataset"
model_dir_root="$assets_root/model"

mkdir -p "$cache_root" "$dataset_dir" "$model_dir_root"

# Redirect common caches into allowed tree.
export XDG_CACHE_HOME="$repo_root/$cache_root/xdg"
export HF_HOME="$repo_root/$cache_root/hf_home"
export HF_HUB_CACHE="$repo_root/$cache_root/hf_hub"
export HUGGINGFACE_HUB_CACHE="$repo_root/$cache_root/hf_hub"
export HF_DATASETS_CACHE="$repo_root/$cache_root/hf_datasets"
export TRANSFORMERS_CACHE="$repo_root/$cache_root/hf_transformers"
export TORCH_HOME="$repo_root/$cache_root/torch"
export MPLCONFIGDIR="$repo_root/$cache_root/matplotlib"
export PIP_CACHE_DIR="$repo_root/$cache_root/pip"
export PIP_DISABLE_PIP_VERSION_CHECK=1

status="success"
exit_code=0
skip_reason="not_applicable"
failure_category="unknown"
command_str=""
decision_reason="Dataset uses repo example audio (examples/example.wav) and training manifest format from docs/finetune.md; model uses smaller official weights openbmb/VoxCPM-0.5B from README.md to minimize download."

dataset_manifest_rel="$dataset_dir/train_manifest.jsonl"
dataset_wav_rel="$dataset_dir/example.wav"
dataset_manifest="$repo_root/$dataset_manifest_rel"
dataset_wav="$repo_root/$dataset_wav_rel"

model_name="${model_id//\//__}"
model_cache_dir="$repo_root/$cache_root/model/$model_name"
model_link="$repo_root/$model_dir_root/$model_name"
model_meta="$repo_root/$model_dir_root/$model_name.meta.json"

log() { echo "$*" | tee -a "$log_file" >/dev/null; }

resolve_python() {
  if [[ -n "$python_exe" ]]; then
    echo "$python_exe"
    return 0
  fi
  if [[ ! -f "$report_path" ]]; then
    return 1
  fi
  "$PYHOST" - <<PY 2>>"$log_file"
import json, os, sys
path = os.environ.get("REPORT_PATH")
try:
  d=json.load(open(path,"r",encoding="utf-8"))
  print(d.get("python_path",""))
except Exception as e:
  sys.exit(1)
PY
}

python_exe="$(REPORT_PATH="$report_path" resolve_python || true)"
python_exe="${python_exe//$'\r'/}"

if [[ -z "$python_exe" || ! -x "$python_exe" ]]; then
  log "[prepare] ERROR: Could not resolve an executable python. Set SCIMLOPSBENCH_PYTHON or provide a valid $report_path with python_path."
  status="failure"
  exit_code=1
  failure_category="missing_report"
fi

dataset_sha256=""
model_sha256=""
model_source="huggingface:$model_id"
model_version="${model_revision:-default}"
dataset_source="repo:examples/example.wav"
dataset_version="${git_commit:-unknown}"

weights_file_rel=""
weights_size_bytes=0
resolved_model_dir=""

if [[ "$status" == "success" ]]; then
  # Dataset prep: copy example wav and write a 1-line manifest.
  if [[ ! -f "examples/example.wav" ]]; then
    log "[prepare] ERROR: Missing examples/example.wav in repo; cannot build minimal dataset."
    status="failure"
    exit_code=1
    failure_category="data"
  else
    mkdir -p "$dataset_dir"
    cp -f "examples/example.wav" "$dataset_wav" >>"$log_file" 2>&1 || true
    cat >"$dataset_manifest" <<EOF
{"audio": "$dataset_wav_rel", "text": "This is a minimal benchmark sample for VoxCPM fine-tuning.", "dataset_id": 0}
EOF
    dataset_sha256="$(sha256sum "$dataset_wav" | awk '{print $1}' 2>>"$log_file" || true)"
    log "[prepare] Dataset ready: $dataset_manifest_rel (wav sha256=$dataset_sha256)"
  fi
fi

verify_model_dir() {
  local d="$1"
  [[ -d "$d" ]] || return 1
  [[ -f "$d/config.json" ]] || return 1
  [[ -f "$d/audiovae.pth" ]] || return 1
  if [[ -f "$d/model.safetensors" ]]; then
    return 0
  fi
  [[ -f "$d/pytorch_model.bin" ]] || return 1
  return 0
}

if [[ "$status" == "success" ]]; then
  # If we already have a recorded sha256 + verified model dir, skip download.
  if [[ -f "$model_meta" ]]; then
    # Prefer the cache directory (real files) over the model link (may be a symlink).
    if [[ -d "$model_cache_dir" ]] && verify_model_dir "$model_cache_dir"; then
      model_sha256="$("$PYHOST" - <<PY 2>>"$log_file"
import json
from pathlib import Path
d=json.loads(Path("$model_meta").read_text(encoding="utf-8"))
print(d.get("weights_sha256",""))
PY
)"
      resolved_model_dir="$model_cache_dir"
      log "[prepare] Reusing cached model at $model_cache_dir (sha256=$model_sha256)"
    elif [[ -e "$model_link" ]] && verify_model_dir "$model_link"; then
      model_sha256="$("$PYHOST" - <<PY 2>>"$log_file"
import json
from pathlib import Path
d=json.loads(Path("$model_meta").read_text(encoding="utf-8"))
print(d.get("weights_sha256",""))
PY
)"
      resolved_model_dir="$model_link"
      log "[prepare] Reusing cached model at $model_link (sha256=$model_sha256)"
    else
      log "[prepare] Cached model metadata exists but model dir is incomplete; re-downloading."
    fi
  fi
fi

if [[ "$status" == "success" && -z "$resolved_model_dir" ]]; then
  mkdir -p "$model_cache_dir"
  command_str="$python_exe - <<'PY' (huggingface_hub.snapshot_download)"
  log "[prepare] Downloading model via huggingface_hub.snapshot_download: $model_id (revision=${model_revision:-default})"
  set +u
  MODEL_ID="$model_id" MODEL_REVISION="$model_revision" MODEL_CACHE_DIR="$model_cache_dir" \
  "$python_exe" - <<'PY' >>"$log_file" 2>&1
import os
import sys
from pathlib import Path

repo_id = os.environ["MODEL_ID"]
revision = os.environ.get("MODEL_REVISION") or None
local_dir = Path(os.environ["MODEL_CACHE_DIR"]).resolve()
local_dir.mkdir(parents=True, exist_ok=True)

try:
    from huggingface_hub import snapshot_download
except Exception as e:
    print(f"ERROR: huggingface_hub not available: {e}")
    sys.exit(2)

def try_download(local_files_only: bool):
    return snapshot_download(
        repo_id,
        revision=revision,
        local_dir=str(local_dir),
        local_dir_use_symlinks=False,
        resume_download=True,
        local_files_only=local_files_only,
    )

try:
    path = try_download(local_files_only=False)
except Exception as e:
    print(f"[prepare] Download failed (will try local_files_only): {e}")
    try:
        path = try_download(local_files_only=True)
    except Exception as e2:
        print(f"[prepare] local_files_only also failed: {e2}")
        sys.exit(3)

print(f"RESOLVED_MODEL_DIR={Path(path).resolve()}")
PY
  dl_ec=$?
  set -u
  if [[ $dl_ec -ne 0 ]]; then
    if [[ $dl_ec -eq 2 ]]; then
      status="failure"; exit_code=1; failure_category="deps"
      log "[prepare] ERROR: missing dependency huggingface_hub in selected python."
    else
      if command -v rg >/dev/null 2>&1 && rg -n "401|403|token" "$log_file" >/dev/null 2>&1; then
        status="failure"; exit_code=1; failure_category="auth_required"
      else
        status="failure"; exit_code=1; failure_category="download_failed"
      fi
      log "[prepare] ERROR: model download failed."
    fi
  else
    # Parse resolved directory from downloader output.
    if command -v rg >/dev/null 2>&1; then
      resolved_model_dir="$(rg -n "^RESOLVED_MODEL_DIR=" "$log_file" | tail -n 1 | sed -E 's/^.*RESOLVED_MODEL_DIR=//' | tr -d '\r' || true)"
    else
      resolved_model_dir="$(grep -E "^RESOLVED_MODEL_DIR=" "$log_file" | tail -n 1 | sed -E 's/^RESOLVED_MODEL_DIR=//' | tr -d '\r' || true)"
    fi
    if [[ -z "$resolved_model_dir" ]]; then
      status="failure"; exit_code=1; failure_category="model"
      log "[prepare] ERROR: download reported success but resolved model dir could not be parsed from logs."
    fi
  fi
fi

if [[ "$status" == "success" ]]; then
  # Link cache dir into benchmark_assets/model/<name>
  if [[ -n "$resolved_model_dir" && -d "$resolved_model_dir" ]]; then
    # Avoid creating a self-referential symlink when resolved_model_dir already points to model_link.
    if [[ "$resolved_model_dir" != "$model_link" ]]; then
      if [[ -e "$model_link" || -L "$model_link" ]]; then
        # If model_link is a real directory (not a symlink), do not delete it blindly.
        if [[ -d "$model_link" && ! -L "$model_link" ]]; then
          log "[prepare] NOTE: $model_link exists as a directory; leaving it in place and using resolved_model_dir=$resolved_model_dir"
        else
          rm -f "$model_link" >>"$log_file" 2>&1 || true
        fi
      fi

      if [[ ! -e "$model_link" && ! -L "$model_link" ]]; then
        if ln -s "$resolved_model_dir" "$model_link" >>"$log_file" 2>&1; then
          resolved_model_dir="$model_link"
        else
          log "[prepare] WARNING: failed to create symlink $model_link -> $resolved_model_dir; using resolved_model_dir=$resolved_model_dir"
        fi
      fi
    fi
  fi

  if ! verify_model_dir "$resolved_model_dir"; then
    status="failure"
    exit_code=1
    failure_category="model"
    log "[prepare] ERROR: resolved model directory is missing required files (config.json, audiovae.pth, model.safetensors/pytorch_model.bin)."
    log "[prepare] resolved_model_dir=$resolved_model_dir"
  else
    # Determine weights file and compute sha256 once; store in meta json.
    if [[ -f "$resolved_model_dir/model.safetensors" ]]; then
      weights_file_rel="model.safetensors"
    else
    weights_file_rel="pytorch_model.bin"
    fi
    weights_size_bytes="$("$PYHOST" - <<PY 2>>"$log_file"
import os
print(os.path.getsize("$resolved_model_dir/$weights_file_rel"))
PY
)"
    if [[ -f "$model_meta" ]]; then
      model_sha256="$("$PYHOST" - <<PY 2>>"$log_file"
import json
from pathlib import Path
d=json.loads(Path("$model_meta").read_text(encoding="utf-8"))
print(d.get("weights_sha256",""))
PY
)"
    fi
    if [[ -z "$model_sha256" ]]; then
      log "[prepare] Computing sha256 for $weights_file_rel (may take time)..."
      model_sha256="$(sha256sum "$resolved_model_dir/$weights_file_rel" | awk '{print $1}' 2>>"$log_file" || true)"
    fi
    cat >"$model_meta" <<EOF
{"model_id":"$model_id","revision":"$model_version","resolved_dir":"$resolved_model_dir","weights_file":"$weights_file_rel","weights_size_bytes":$weights_size_bytes,"weights_sha256":"$model_sha256"}
EOF
    log "[prepare] Model ready: $resolved_model_dir (weights=$weights_file_rel sha256=$model_sha256)"
  fi
fi

# Record the final model path that downstream stages should use.
model_path_out="$resolved_model_dir"
if [[ -n "$model_path_out" && "$model_path_out" == "$repo_root/"* ]]; then
  model_path_out="${model_path_out#"$repo_root/"}"
fi

# Write results.json (always).
STATUS="$status" SKIP_REASON="$skip_reason" EXIT_CODE="$exit_code" STAGE="$stage" TASK="$task" \
COMMAND="$command_str" TIMEOUT_SEC="$timeout_sec" FRAMEWORK="$framework" FAILURE_CATEGORY="$failure_category" \
DATASET_PATH="$dataset_manifest_rel" DATASET_SOURCE="$dataset_source" DATASET_VERSION="$dataset_version" DATASET_SHA256="$dataset_sha256" \
MODEL_PATH="$model_path_out" MODEL_SOURCE="$model_source" MODEL_VERSION="$model_version" MODEL_SHA256="$model_sha256" \
PYTHON_EXE="$python_exe" GIT_COMMIT="$git_commit" DECISION_REASON="$decision_reason" MODEL_ID="$model_id" \
WEIGHTS_FILE="$weights_file_rel" WEIGHTS_SIZE_BYTES="$weights_size_bytes" SAMPLE_RATE="$sample_rate" \
LOG_FILE="$log_file" RESULTS_JSON="$results_json" \
"$PYHOST" - <<PY 2>>"$log_file"
import json
import os
from datetime import datetime, timezone
from pathlib import Path

def tail(path: Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

payload = {
  "status": os.environ.get("STATUS","failure"),
  "skip_reason": os.environ.get("SKIP_REASON","unknown"),
  "exit_code": int(os.environ.get("EXIT_CODE","1")),
  "stage": os.environ.get("STAGE","prepare"),
  "task": os.environ.get("TASK","download"),
  "command": os.environ.get("COMMAND", ""),
  "timeout_sec": int(os.environ.get("TIMEOUT_SEC","1200") or 1200),
  "framework": os.environ.get("FRAMEWORK","unknown"),
  "assets": {
    "dataset": {
      "path": os.environ.get("DATASET_PATH",""),
      "source": os.environ.get("DATASET_SOURCE",""),
      "version": os.environ.get("DATASET_VERSION",""),
      "sha256": os.environ.get("DATASET_SHA256",""),
    },
    "model": {
      "path": os.environ.get("MODEL_PATH",""),
      "source": os.environ.get("MODEL_SOURCE",""),
      "version": os.environ.get("MODEL_VERSION",""),
      "sha256": os.environ.get("MODEL_SHA256",""),
    },
  },
  "meta": {
    "python": os.environ.get("PYTHON_EXE",""),
    "git_commit": os.environ.get("GIT_COMMIT",""),
    "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "env_vars": {k: os.environ.get(k,"") for k in [
      "HF_HOME","HF_HUB_CACHE","HUGGINGFACE_HUB_CACHE","HF_DATASETS_CACHE","TRANSFORMERS_CACHE","TORCH_HOME","XDG_CACHE_HOME","PIP_CACHE_DIR",
      "SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","VOXCPM_MODEL_ID","VOXCPM_MODEL_REVISION"
    ]},
    "decision_reason": os.environ.get("DECISION_REASON",""),
    "prepared": {
      "model_id": os.environ.get("MODEL_ID",""),
      "model_revision": os.environ.get("MODEL_VERSION",""),
      "weights_file": os.environ.get("WEIGHTS_FILE",""),
      "weights_size_bytes": int(os.environ.get("WEIGHTS_SIZE_BYTES","0") or 0),
      "sample_rate": int(os.environ.get("SAMPLE_RATE","16000") or 16000),
    },
  },
  "failure_category": os.environ.get("FAILURE_CATEGORY","unknown"),
  "error_excerpt": tail(Path(os.environ["LOG_FILE"])),
}

Path(os.environ["RESULTS_JSON"]).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY

# Exit with stage semantics.
exit "$exit_code"
