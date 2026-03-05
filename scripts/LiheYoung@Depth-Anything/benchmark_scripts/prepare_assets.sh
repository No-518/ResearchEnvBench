#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model) into:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Outputs (always written, even on failure):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Optional:
  --encoder <vits|vitb|vitl>   Default: vits (smallest)
  --report-path <path>        Override report path (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)

Environment:
  SCIMLOPSBENCH_PYTHON         Override python interpreter used for downloads
  HF_TOKEN / HF_AUTH_TOKEN     If needed for gated models (not needed by default here)
EOF
}

encoder="vits"
report_path=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --encoder)
      encoder="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

stage_dir="$repo_root/build_output/prepare"
mkdir -p "$stage_dir"
log_file="$stage_dir/log.txt"
results_json="$stage_dir/results.json"

sys_py="$(command -v python3 || command -v python || true)"

status="failure"
skip_reason="unknown"
exit_code=1
failure_category="unknown"
command_str="benchmark_scripts/prepare_assets.sh --encoder $encoder"
decision_reason="Using README 'Running' example images as dataset and Hugging Face repo LiheYoung/depth_anything_${encoder}14 as minimal model."

dataset_path=""
dataset_source=""
dataset_version=""
dataset_sha256=""
model_path=""
model_source=""
model_version=""
model_sha256=""

write_results() {
  if [[ -z "$sys_py" ]]; then
    printf '%s\n' "FATAL: python/python3 not found to write structured JSON; writing minimal results." >>"$log_file"
    cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh --encoder ${encoder}",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "python/python3 not found"},
  "failure_category": "deps",
  "error_excerpt": "python/python3 not found on PATH"
}
JSON
    return
  fi
  STAGE_DIR="$stage_dir" \
  STATUS="$status" \
  SKIP_REASON="$skip_reason" \
  EXIT_CODE="$exit_code" \
  FAILURE_CATEGORY="$failure_category" \
  COMMAND_STR="$command_str" \
  DECISION_REASON="$decision_reason" \
  DATASET_PATH="$dataset_path" \
  DATASET_SOURCE="$dataset_source" \
  DATASET_VERSION="$dataset_version" \
  DATASET_SHA256="$dataset_sha256" \
  MODEL_PATH="$model_path" \
  MODEL_SOURCE="$model_source" \
  MODEL_VERSION="$model_version" \
  MODEL_SHA256="$model_sha256" \
  "$sys_py" - <<'PY'
import datetime as dt
import json
import os
import pathlib
import subprocess

stage_dir = pathlib.Path(os.environ["STAGE_DIR"])
log_file = stage_dir / "log.txt"
results_json = stage_dir / "results.json"
repo = stage_dir.parent.parent

def git_commit(root: pathlib.Path) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if p.returncode == 0:
            return p.stdout.strip()
    except Exception:
        pass
    return ""

def tail(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""

payload = {
    "status": os.environ.get("STATUS", "failure"),
    "skip_reason": os.environ.get("SKIP_REASON", "unknown"),
    "exit_code": int(os.environ.get("EXIT_CODE", "1") or "1"),
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
        "python": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        "git_commit": git_commit(repo),
        "env_vars": {
            k: os.environ.get(k, "")
            for k in [
                "SCIMLOPSBENCH_REPORT",
                "SCIMLOPSBENCH_PYTHON",
                "HF_HOME",
                "HUGGINGFACE_HUB_CACHE",
                "TORCH_HOME",
                "HF_TOKEN",
                "HF_AUTH_TOKEN",
            ]
            if os.environ.get(k)
        },
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "timestamp_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail(log_file),
}

results_json.parent.mkdir(parents=True, exist_ok=True)
tmp = results_json.with_suffix(results_json.suffix + ".tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(results_json)
PY
}

trap 'write_results' EXIT

{
  echo "== prepare stage =="
  echo "encoder: $encoder"
  echo "report_path_override: ${report_path:-<none>}"
  echo "repo_root: $repo_root"
} >"$log_file"

mkdir -p benchmark_assets/cache benchmark_assets/dataset benchmark_assets/model

# Resolve python (must be the environment intended for repo execution).
if [[ -z "$sys_py" ]]; then
  failure_category="deps"
  echo "python/python3 not found on PATH; cannot resolve report python." >>"$log_file"
  exit 1
fi

if [[ -n "${report_path:-}" ]]; then
  export SCIMLOPSBENCH_REPORT="$report_path"
fi

resolved_py="$("$sys_py" benchmark_scripts/runner.py --stage prepare --task download --report-path "${SCIMLOPSBENCH_REPORT:-}" --print-python 2>>"$log_file" || true)"
if [[ -z "$resolved_py" ]]; then
  failure_category="missing_report"
  echo "Failed to resolve python from report.json; set SCIMLOPSBENCH_PYTHON or provide a valid report." >>"$log_file"
  exit 1
fi

export SCIMLOPSBENCH_PYTHON="${SCIMLOPSBENCH_PYTHON:-$resolved_py}"

# Force caches to stay within benchmark_assets/.
export HF_HOME="$repo_root/benchmark_assets/cache/huggingface"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HUB_DISABLE_TELEMETRY=1

echo "Resolved python: $SCIMLOPSBENCH_PYTHON" >>"$log_file"
echo "HF_HOME: $HF_HOME" >>"$log_file"
echo "TORCH_HOME: $TORCH_HOME" >>"$log_file"

# Dataset: use a single bundled example image to guarantee a 1-step inference run.
example_src="assets/examples/demo1.png"
if [[ ! -f "$example_src" ]]; then
  failure_category="data"
  echo "Expected example image not found: $example_src" >>"$log_file"
  exit 1
fi

dataset_cache_dir="benchmark_assets/cache/dataset"
dataset_dir="benchmark_assets/dataset/examples_1"
mkdir -p "$dataset_cache_dir" "$dataset_dir"

dataset_cache_file="$dataset_cache_dir/demo1.png"
cp -f "$example_src" "$dataset_cache_file"
cp -f "$dataset_cache_file" "$dataset_dir/demo1.png"

dataset_path="$repo_root/$dataset_dir"
dataset_source="repo://assets/examples/demo1.png"
dataset_version="bundled"
dataset_sha256="$("$sys_py" - <<'PY' "$dataset_cache_file" 2>>"$log_file"
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
h = hashlib.sha256()
h.update(p.read_bytes())
print(h.hexdigest())
PY
)"

echo "Dataset prepared: $dataset_path" >>"$log_file"

# Model: download the smallest HF repo by default.
repo_id="LiheYoung/depth_anything_${encoder}14"
model_local_dir="$repo_root/benchmark_assets/model/${repo_id//\//__}"
model_cache_dir="$repo_root/benchmark_assets/cache/huggingface/hub"

echo "Model repo_id: $repo_id" >>"$log_file"
echo "Model local_dir: $model_local_dir" >>"$log_file"

set +e
"$SCIMLOPSBENCH_PYTHON" - <<PY >>"$log_file" 2>&1
import os
import pathlib
import sys

repo_id = ${repo_id@Q}
cache_dir = pathlib.Path(${model_cache_dir@Q})
local_dir = pathlib.Path(${model_local_dir@Q})
revision = "main"

try:
    from huggingface_hub import snapshot_download
except Exception as e:
    print(f"ERROR: huggingface_hub import failed: {e}")
    sys.exit(10)

cache_dir.mkdir(parents=True, exist_ok=True)
local_dir.mkdir(parents=True, exist_ok=True)

try:
    path = snapshot_download(
        repo_id=repo_id,
        repo_type="model",
        revision=revision,
        cache_dir=str(cache_dir),
        local_dir=str(local_dir),
        local_dir_use_symlinks=False,
        resume_download=True,
    )
    print(f"snapshot_download_ok: {path}")
except Exception as e:
    print(f"snapshot_download_failed: {e}")
    # non-zero to let caller decide about offline reuse
    sys.exit(20)
PY
dl_rc="$?"
set -e

if [[ "$dl_rc" == "10" ]]; then
  failure_category="deps"
  echo "huggingface_hub is required (also required by run.py) but could not be imported." >>"$log_file"
  exit 1
fi

if [[ "$dl_rc" != "0" ]]; then
  echo "Model download failed (rc=$dl_rc). Checking for offline reuse..." >>"$log_file"
fi

if [[ ! -d "$model_local_dir" ]]; then
  failure_category="model"
  echo "Downloader reported completion but model directory does not exist: $model_local_dir" >>"$log_file"
  exit 1
fi

# Verify expected artifacts exist.
if ! ls "$model_local_dir"/* >/dev/null 2>&1; then
  failure_category="model"
  echo "Model directory exists but appears empty: $model_local_dir" >>"$log_file"
  exit 1
fi

model_path="$model_local_dir"
model_source="hf://$repo_id"
model_version="main"

model_sha256="$("$sys_py" - <<'PY' "$model_local_dir" 2>>"$log_file"
import hashlib, os, pathlib, sys

root = pathlib.Path(sys.argv[1])
weights = []
for pat in ("*.safetensors","*.bin","*.pth","*.pt"):
    weights.extend(root.rglob(pat))
weights = [p for p in weights if p.is_file()]
weights.sort(key=lambda p: (p.stat().st_size, str(p)), reverse=True)

if not weights:
    print("")
    raise SystemExit(0)

h = hashlib.sha256()
for p in weights[:5]:  # hash up to 5 largest files for a stable fingerprint
    h.update(str(p.relative_to(root)).encode("utf-8"))
    h.update(b"\0")
    h.update(p.read_bytes())
print(h.hexdigest())
PY
)"

if [[ -z "$model_sha256" ]]; then
  failure_category="model"
  echo "Could not locate model weight files under: $model_local_dir" >>"$log_file"
  exit 1
fi

status="success"
exit_code=0
failure_category="unknown"

echo "Model prepared: $model_path" >>"$log_file"
