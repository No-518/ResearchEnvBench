#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare minimal benchmark assets (dataset + model) in a reproducible way.

This repo choice (auto):
  - Task: speech enhancement (FRCRN_SE_16K)
  - Dataset: repo sample audio (clearvoice/samples/input.wav)
  - Model: HuggingFace model repo alibabasglab/FRCRN_SE_16K (downloaded via huggingface_hub)

Outputs (always written):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Environment:
  SCIMLOPSBENCH_REPORT            Override report.json path (default: /opt/scimlopsbench/report.json)
  SCIMLOPSBENCH_PYTHON            Override python executable
  SCIMLOPSBENCH_OFFLINE=1         Force offline (local cache only)
  HF_TOKEN / HUGGINGFACE_HUB_TOKEN  HuggingFace auth token (if needed)
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/prepare"
log_path="$stage_dir/log.txt"
results_path="$stage_dir/results.json"

cache_root="$repo_root/benchmark_assets/cache"
dataset_root="$repo_root/benchmark_assets/dataset"
model_root="$repo_root/benchmark_assets/model"

mkdir -p "$stage_dir" "$cache_root" "$dataset_root" "$model_root"
: >"$log_path"

stage_status="failure"
failure_category="unknown"
skip_reason="unknown"
exit_code=1
decision_reason=""
command_str=""

dataset_path=""
dataset_source=""
dataset_version=""
dataset_sha256=""

model_path=""
model_source=""
model_version=""
model_sha256=""
model_checkpoint_path=""

python_exe=""
python_override=""

git_commit="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)"
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"

note() { echo "[prepare] $*" >>"$log_path"; }

write_results() {
  STAGE_STATUS="$stage_status" SKIP_REASON="$skip_reason" EXIT_CODE="$exit_code" FAILURE_CATEGORY="$failure_category" \
  COMMAND_STR="$command_str" DECISION_REASON="$decision_reason" PYTHON_EXE="$python_exe" GIT_COMMIT="$git_commit" \
  DATASET_PATH="$dataset_path" DATASET_SOURCE="$dataset_source" DATASET_VERSION="$dataset_version" DATASET_SHA256="$dataset_sha256" \
  MODEL_PATH="$model_path" MODEL_SOURCE="$model_source" MODEL_VERSION="$model_version" MODEL_SHA256="$model_sha256" \
  MODEL_CHECKPOINT_PATH="$model_checkpoint_path" LOG_PATH="$log_path" RESULTS_PATH="$results_path" \
  python - <<'PY'
import json
import os
import pathlib
import time

def tail(path: str, n: int = 220) -> str:
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

payload = {
    "status": os.environ.get("STAGE_STATUS", "failure"),
    "skip_reason": os.environ.get("SKIP_REASON", "unknown"),
    "exit_code": int(os.environ.get("EXIT_CODE", "1")),
    "stage": "prepare",
    "task": "download",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": 1200,
    "framework": "unknown",
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
        "python": os.environ.get("PYTHON_EXE", ""),
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.getenv("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_OFFLINE": os.getenv("SCIMLOPSBENCH_OFFLINE", ""),
            "HF_HOME": os.getenv("HF_HOME", ""),
            "HUGGINGFACE_HUB_CACHE": os.getenv("HUGGINGFACE_HUB_CACHE", ""),
        },
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "model_checkpoint_path": os.environ.get("MODEL_CHECKPOINT_PATH", ""),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail(os.environ.get("LOG_PATH", "")),
}

pathlib.Path(os.environ["RESULTS_PATH"]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY
}

fail() {
  failure_category="$1"; shift
  note "ERROR ($failure_category): $*"
  stage_status="failure"
  exit_code=1
  write_results
  exit 1
}

on_unhandled_error() {
  local ec=$?
  trap - ERR
  set +e
  stage_status="failure"
  exit_code=1
  failure_category="${failure_category:-runtime}"
  decision_reason="${decision_reason:-Unhandled error}"
  command_str="${command_str:-$0}"
  write_results || true
  exit "$ec"
}
trap on_unhandled_error ERR

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_override="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      decision_reason="Unknown CLI argument."
      command_str="$0 $*"
      fail "args_unknown" "Unknown argument: $1"
      ;;
  esac
done

if [[ -n "$python_override" ]]; then
  python_exe="$python_override"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  python_exe="$SCIMLOPSBENCH_PYTHON"
elif [[ -f "$report_path" ]]; then
  python_exe="$(python - <<PY 2>>"$log_path" || true
import json
try:
  data=json.load(open("${report_path}","r",encoding="utf-8"))
  print(data.get("python_path","") or "")
except Exception:
  print("")
PY
)"
fi

note "repo_root=$repo_root"
note "report_path=$report_path"
note "python_exe=${python_exe:-<empty>}"

if [[ -z "$python_exe" ]]; then
  decision_reason="No --python/SCIMLOPSBENCH_PYTHON and report.json missing/invalid."
  fail "missing_report" "Could not resolve python executable from report.json."
fi

if ! "$python_exe" -c 'import sys; print(sys.executable)' >>"$log_path" 2>&1; then
  decision_reason="Resolved python executable is not runnable."
  fail "deps" "Python not executable: $python_exe"
fi

# Keep caches inside benchmark_assets/cache.
export HF_HOME="$cache_root/hf"
export HUGGINGFACE_HUB_CACHE="$cache_root/hf"
mkdir -p "$HF_HOME"

# If previous results.json matches current artifacts (sha256), skip download/copy.
if [[ -f "$results_path" ]]; then
  reuse_ok="$(RESULTS_PATH="$results_path" python - <<'PY' 2>/dev/null || true
import hashlib, json, os, pathlib, sys

rp = pathlib.Path(os.environ["RESULTS_PATH"])
try:
    data = json.loads(rp.read_text(encoding="utf-8"))
except Exception:
    print("0"); sys.exit(0)

if data.get("status") != "success":
    print("0"); sys.exit(0)

assets = data.get("assets") or {}
ds = assets.get("dataset") or {}
md = assets.get("model") or {}
meta = data.get("meta") or {}

ds_path = pathlib.Path(ds.get("path",""))
md_root = pathlib.Path(md.get("path",""))
ckpt_path = pathlib.Path(meta.get("model_checkpoint_path","")) if meta.get("model_checkpoint_path") else None

def sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()

ok = True
if not ds_path.is_file():
    ok = False
elif ds.get("sha256") and sha256(ds_path) != ds.get("sha256"):
    ok = False

ckpt = None
if ckpt_path and ckpt_path.is_file():
    ckpt = ckpt_path
else:
    if (md_root / "last_best_checkpoint.pt").is_file():
        ckpt = md_root / "last_best_checkpoint.pt"
    elif (md_root / "last_best_checkpoint").is_file():
        try:
            name = (md_root / "last_best_checkpoint").read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
            if name and (md_root / name).is_file():
                ckpt = md_root / name
        except Exception:
            ckpt = None
    if ckpt is None:
        pts = list(md_root.glob("*.pt"))
        if pts:
            ckpt = pts[0]

if ckpt is None or not ckpt.exists():
    ok = False
elif md.get("sha256") and sha256(ckpt) != md.get("sha256"):
    ok = False

print("1" if ok else "0")
PY
)"
  if [[ "$reuse_ok" == "1" ]]; then
    note "Reuse: dataset/model sha256 match previous prepare results; skipping download."
    stage_status="success"
    exit_code=0
    failure_category="none"
    decision_reason="Reused previously prepared dataset/model (sha256 match)."
    command_str="prepare_assets.sh (reuse)"
    # Keep existing asset fields by reading them back (for completeness in results.json rewrite).
    dataset_path="$(RESULTS_PATH="$results_path" python - <<'PY' 2>/dev/null || true
import json, os, pathlib
d=json.loads(pathlib.Path(os.environ["RESULTS_PATH"]).read_text(encoding="utf-8"))
print(d.get("assets",{}).get("dataset",{}).get("path",""))
PY
)"
    dataset_source="$(RESULTS_PATH="$results_path" python - <<'PY' 2>/dev/null || true
import json, os, pathlib
d=json.loads(pathlib.Path(os.environ["RESULTS_PATH"]).read_text(encoding="utf-8"))
print(d.get("assets",{}).get("dataset",{}).get("source",""))
PY
)"
    dataset_version="$(RESULTS_PATH="$results_path" python - <<'PY' 2>/dev/null || true
import json, os, pathlib
d=json.loads(pathlib.Path(os.environ["RESULTS_PATH"]).read_text(encoding="utf-8"))
print(d.get("assets",{}).get("dataset",{}).get("version",""))
PY
)"
    dataset_sha256="$(RESULTS_PATH="$results_path" python - <<'PY' 2>/dev/null || true
import json, os, pathlib
d=json.loads(pathlib.Path(os.environ["RESULTS_PATH"]).read_text(encoding="utf-8"))
print(d.get("assets",{}).get("dataset",{}).get("sha256",""))
PY
)"
    model_path="$(RESULTS_PATH="$results_path" python - <<'PY' 2>/dev/null || true
import json, os, pathlib
d=json.loads(pathlib.Path(os.environ["RESULTS_PATH"]).read_text(encoding="utf-8"))
print(d.get("assets",{}).get("model",{}).get("path",""))
PY
)"
    model_source="$(RESULTS_PATH="$results_path" python - <<'PY' 2>/dev/null || true
import json, os, pathlib
d=json.loads(pathlib.Path(os.environ["RESULTS_PATH"]).read_text(encoding="utf-8"))
print(d.get("assets",{}).get("model",{}).get("source",""))
PY
)"
    model_version="$(RESULTS_PATH="$results_path" python - <<'PY' 2>/dev/null || true
import json, os, pathlib
d=json.loads(pathlib.Path(os.environ["RESULTS_PATH"]).read_text(encoding="utf-8"))
print(d.get("assets",{}).get("model",{}).get("version",""))
PY
)"
    model_sha256="$(RESULTS_PATH="$results_path" python - <<'PY' 2>/dev/null || true
import json, os, pathlib
d=json.loads(pathlib.Path(os.environ["RESULTS_PATH"]).read_text(encoding="utf-8"))
print(d.get("assets",{}).get("model",{}).get("sha256",""))
PY
)"
    model_checkpoint_path="$(RESULTS_PATH="$results_path" python - <<'PY' 2>/dev/null || true
import json, os, pathlib
d=json.loads(pathlib.Path(os.environ["RESULTS_PATH"]).read_text(encoding="utf-8"))
print(d.get("meta",{}).get("model_checkpoint_path",""))
PY
)"
    write_results
    exit 0
  fi
fi

# Dataset: use repo-provided sample audio.
sample_src="$repo_root/clearvoice/samples/input.wav"
if [[ ! -f "$sample_src" ]]; then
  decision_reason="Expected sample audio missing at clearvoice/samples/input.wav."
  fail "data" "Missing sample audio: $sample_src"
fi

dataset_file="$dataset_root/input.wav"
note "Preparing dataset: $sample_src -> $dataset_file"
cp -f "$sample_src" "$dataset_file"
dataset_path="$dataset_file"
dataset_source="repo://clearvoice/samples/input.wav"
dataset_version="$git_commit"
dataset_sha256="$(sha256sum "$dataset_file" | awk '{print $1}')"

# Minimal training list files (1 sample) for train.py.
echo "$dataset_file $dataset_file" >"$dataset_root/train.scp"
echo "$dataset_file $dataset_file" >"$dataset_root/cv.scp"

# Model: download from HuggingFace with huggingface_hub (robust local_dir).
model_id="alibabasglab/FRCRN_SE_16K"
model_source="hf://${model_id}"
model_version="main"
cache_dir="$cache_root/hf_models/FRCRN_SE_16K"
mkdir -p "$cache_dir"

export MODEL_ID="$model_id"
export MODEL_CACHE_DIR="$cache_dir"
export MODEL_REVISION="main"

note "Downloading model: $model_id -> $cache_dir"
command_str="$python_exe -c \"from huggingface_hub import snapshot_download; snapshot_download(repo_id='${model_id}', revision='main', local_dir='${cache_dir}', local_dir_use_symlinks=False, allow_patterns=['*.pt','last_best_checkpoint*','last_checkpoint*','*.json','*.yaml','*.txt'], resume_download=True, local_files_only=<auto>)\""

resolved_cache_dir="$("$python_exe" - <<'PY' 2>>"$log_path" || true
import os
from pathlib import Path

repo_id = os.environ.get("MODEL_ID")
local_dir = Path(os.environ.get("MODEL_CACHE_DIR"))
revision = os.environ.get("MODEL_REVISION", "main")
offline = os.environ.get("SCIMLOPSBENCH_OFFLINE", "") == "1"

try:
    from huggingface_hub import snapshot_download
except Exception as e:
    print("")
    raise SystemExit(f"huggingface_hub import failed: {e}")

allow_patterns = ["*.pt", "last_best_checkpoint*", "last_checkpoint*", "*.json", "*.yaml", "*.txt"]

def do(local_files_only: bool) -> str:
    p = snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=str(local_dir),
        local_dir_use_symlinks=False,
        allow_patterns=allow_patterns,
        resume_download=True,
        local_files_only=local_files_only,
    )
    return str(p)

try:
    if offline:
        print(do(local_files_only=True))
    else:
        try:
            print(do(local_files_only=False))
        except Exception:
            print(do(local_files_only=True))
except Exception as e:
    print("")
    raise SystemExit(str(e))
PY
)" || true

if [[ -z "$resolved_cache_dir" ]]; then
  if grep -E -q "(huggingface_hub import failed|No module named|ModuleNotFoundError)" "$log_path" 2>/dev/null; then
    decision_reason="huggingface_hub missing in the configured python env."
    fail "deps" "huggingface_hub not available in $python_exe"
  fi
  if grep -E -q "(401|403|GatedRepoError|Unauthorized|Forbidden|requires.*token|You must be logged in|Invalid user token|token.*required)" "$log_path" 2>/dev/null; then
    decision_reason="HuggingFace authentication required (set HF_TOKEN or HUGGINGFACE_HUB_TOKEN)."
    fail "auth_required" "Model download requires authentication for $model_id"
  fi
  if [[ "${SCIMLOPSBENCH_OFFLINE:-}" == "1" ]]; then
    decision_reason="Offline mode enabled and model cache missing."
    fail "download_failed" "Offline mode: could not find cached model under $cache_dir"
  fi
  decision_reason="Model download failed; see log for details."
  fail "download_failed" "Failed to download model $model_id (cache_dir=$cache_dir)."
fi

note "snapshot_download resolved local_dir=$resolved_cache_dir"

# Link/copy into benchmark_assets/model/.
model_dst="$model_root/FRCRN_SE_16K"
rm -rf "$model_dst" 2>/dev/null || true
if ln -s "$resolved_cache_dir" "$model_dst" 2>>"$log_path"; then
  note "Linked model dir: $model_dst -> $resolved_cache_dir"
else
  note "Symlink failed; copying model dir into $model_dst"
  mkdir -p "$model_dst"
  cp -a "$resolved_cache_dir/." "$model_dst/"
fi

model_path="$model_dst"

# Robustly locate/verify checkpoint artifact for hashing & downstream scripts.
if [[ -f "$model_dst/last_best_checkpoint.pt" ]]; then
  model_checkpoint_path="$model_dst/last_best_checkpoint.pt"
elif [[ -f "$model_dst/last_best_checkpoint" ]]; then
  ckpt_name="$(head -n 1 "$model_dst/last_best_checkpoint" | tr -d '\r' | tr -d '\n')"
  if [[ -n "$ckpt_name" && -f "$model_dst/$ckpt_name" ]]; then
    model_checkpoint_path="$model_dst/$ckpt_name"
  fi
fi

if [[ -z "$model_checkpoint_path" ]]; then
  model_checkpoint_path="$(find "$model_dst" -maxdepth 2 -type f -name "*.pt" | head -n 1 || true)"
fi

if [[ -z "$model_checkpoint_path" || ! -f "$model_checkpoint_path" ]]; then
  decision_reason="Download reported success, but no checkpoint artifacts were found/verified."
  note "Model dir listing:"
  ls -la "$model_dst" >>"$log_path" 2>&1 || true
  fail "model" "Could not locate checkpoint under $model_dst (searched root=$model_dst)."
fi

# Ensure repo inference loader compatibility:
# - train/*/inference.py expects a pointer file `last_best_checkpoint` with a relative checkpoint filename.
# - training scripts commonly reference `last_best_checkpoint.pt`.
ckpt_base="$(basename "$model_checkpoint_path")"
if [[ ! -f "$model_dst/last_best_checkpoint" ]]; then
  printf '%s\n' "$ckpt_base" >"$model_dst/last_best_checkpoint"
  note "Wrote pointer: $model_dst/last_best_checkpoint -> $ckpt_base"
fi
if [[ ! -f "$model_dst/last_checkpoint" ]]; then
  printf '%s\n' "$ckpt_base" >"$model_dst/last_checkpoint"
  note "Wrote pointer: $model_dst/last_checkpoint -> $ckpt_base"
fi
if [[ ! -f "$model_dst/last_best_checkpoint.pt" ]]; then
  if ln -s "$ckpt_base" "$model_dst/last_best_checkpoint.pt" 2>>"$log_path"; then
    note "Linked: $model_dst/last_best_checkpoint.pt -> $ckpt_base"
  else
    cp -f "$model_checkpoint_path" "$model_dst/last_best_checkpoint.pt"
    note "Copied: $model_dst/last_best_checkpoint.pt from $model_checkpoint_path"
  fi
  model_checkpoint_path="$model_dst/last_best_checkpoint.pt"
fi

model_sha256="$(sha256sum "$model_checkpoint_path" | awk '{print $1}')"

stage_status="success"
exit_code=0
failure_category="none"
skip_reason="unknown"
decision_reason="Prepared minimal assets for FRCRN_SE_16K: dataset from repo sample + model from HuggingFace."

write_results
exit 0
