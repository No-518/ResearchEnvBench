#!/usr/bin/env bash
set -euo pipefail

# Asset preparation: dataset + minimal model download.
# Writes:
#   build_output/prepare/log.txt
#   build_output/prepare/results.json
#
# Populates (runtime artifacts only):
#   benchmark_assets/cache/{dataset,model}/...
#   benchmark_assets/dataset/...
#   benchmark_assets/model/...

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage="prepare"
timeout_sec=1200
out_dir="$repo_root/build_output/$stage"
mkdir -p "$out_dir"
log_path="$out_dir/log.txt"
results_path="$out_dir/results.json"

util_python=""
if command -v python >/dev/null 2>&1; then
  util_python="python"
elif command -v python3 >/dev/null 2>&1; then
  util_python="python3"
else
  echo "[prepare] ERROR: python (or python3) not found in PATH"
  exit 1
fi

exec > >(tee "$log_path") 2>&1

echo "[prepare] repo_root=$repo_root"

report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_override="${SCIMLOPSBENCH_PYTHON:-}"

resolved_python=""
python_resolution=""
python_exec=""
python_version=""
git_commit="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)"
framework="unknown"
status="failure"
exit_code=1
skip_reason="unknown"
failure_category="unknown"
decision_reason=""
dataset_out_dir=""
dataset_source=""
dataset_version=""
dataset_sha256=""
model_out_dir=""
model_source=""
model_version=""
model_sha256=""
dataset_list_1row=""
bench_config_path=""

ensure_results_on_exit() {
  local rc=$?
  if [[ -f "$results_path" ]]; then
    return 0
  fi
  local excerpt
  excerpt="$(tail -n 220 "$log_path" 2>/dev/null || true)"
  "$util_python" - <<'PY' \
    "$results_path" "$rc" "$timeout_sec" "$framework" "$python_exec" "$python_version" "$git_commit" \
    "$report_path" "$python_resolution" "$decision_reason" "$skip_reason" "$failure_category" "$excerpt" \
    "$dataset_out_dir" "$dataset_source" "$dataset_version" "$dataset_sha256" \
    "$model_out_dir" "$model_source" "$model_version" "$model_sha256"
import json, sys
from pathlib import Path

(
  results_path, rc, timeout_sec, framework, python_exec, python_version, git_commit,
  report_path, python_resolution, decision_reason, skip_reason, failure_category, excerpt,
  dataset_path, dataset_source, dataset_version, dataset_sha256,
  model_path, model_source, model_version, model_sha256,
) = sys.argv[1:]

payload = {
  "status": "failure",
  "skip_reason": skip_reason or "unknown",
  "exit_code": int(rc) if str(rc).isdigit() and int(rc) != 0 else 1,
  "stage": "prepare",
  "task": "download",
  "command": "bash benchmark_scripts/prepare_assets.sh",
  "timeout_sec": int(timeout_sec) if str(timeout_sec).isdigit() else 1200,
  "framework": framework or "unknown",
  "assets": {
    "dataset": {"path": dataset_path, "source": dataset_source, "version": dataset_version, "sha256": dataset_sha256},
    "model": {"path": model_path, "source": model_source, "version": model_version, "sha256": model_sha256},
  },
  "meta": {
    "python": python_exec,
    "python_version": python_version,
    "git_commit": git_commit,
    "env_vars": {},
    "decision_reason": decision_reason or "prepare_assets.sh exited before writing results.json",
    "report_path": report_path,
    "python_resolution": python_resolution,
  },
  "failure_category": failure_category or "unknown",
  "error_excerpt": excerpt or "",
}

Path(results_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

trap ensure_results_on_exit EXIT

if [[ -n "$python_override" ]]; then
  resolved_python="$python_override"
  python_resolution="SCIMLOPSBENCH_PYTHON"
elif [[ -f "$report_path" ]]; then
  resolved_python="$(
    "$util_python" - <<'PY' "$report_path" 2>/dev/null || true
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
try:
  d=json.loads(p.read_text(encoding="utf-8"))
  print(d.get("python_path",""))
except Exception:
  print("")
PY
  )"
  if [[ -n "$resolved_python" ]]; then
    python_resolution="report.json"
  fi
fi

if [[ -z "$resolved_python" ]]; then
  echo "[prepare] ERROR: unable to resolve python (missing $report_path and no SCIMLOPSBENCH_PYTHON)."
  "$util_python" - <<'PY' "$results_path" "$report_path" "$git_commit"
import json, sys, datetime
p=sys.argv[1]
report_path=sys.argv[2]
git_commit=sys.argv[3]
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "bash benchmark_scripts/prepare_assets.sh",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": {"python": "", "python_version": "", "git_commit": git_commit, "env_vars": {}, "decision_reason": "requires agent report python_path or SCIMLOPSBENCH_PYTHON", "report_path": report_path},
  "failure_category": "missing_report",
  "error_excerpt": f"Missing report or python override. report_path={report_path}",
}
with open(p, "w", encoding="utf-8") as f:
  json.dump(payload, f, ensure_ascii=False, indent=2)
PY
  exit 1
fi

python_exec="$("$resolved_python" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
python_version="$("$resolved_python" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"

if "$resolved_python" -c 'import torch' >/dev/null 2>&1; then
  framework="pytorch"
fi

assets_root="$repo_root/benchmark_assets"
cache_root="$assets_root/cache"
dataset_dir="$assets_root/dataset"
model_dir="$assets_root/model"

mkdir -p "$cache_root/dataset" "$cache_root/model" "$dataset_dir" "$model_dir"

dataset_src_parquet="$repo_root/examples/music_generation/data/samples/parquet/parquet_000000000.tar"
dataset_src_list="$repo_root/examples/music_generation/data/samples/parquet/data.list"

dataset_cache_dir="$cache_root/dataset/inspiremusic_samples"
dataset_out_dir="$dataset_dir/inspiremusic_samples"
mkdir -p "$dataset_cache_dir" "$dataset_out_dir"

dataset_cache_parquet="$dataset_cache_dir/parquet_000000000.parquet"
dataset_out_parquet="$dataset_out_dir/parquet_000000000.parquet"
dataset_out_parquet_1row="$dataset_out_dir/parquet_000000000_1row.parquet"
dataset_list_1row="$dataset_out_dir/data_1row.list"

dataset_source="repo:examples/music_generation/data/samples/parquet/parquet_000000000.tar"
dataset_version="local"
dataset_ok=1

echo "[prepare] Dataset source: $dataset_src_parquet"

if [[ ! -f "$dataset_src_parquet" ]]; then
  echo "[prepare] ERROR: sample parquet not found at $dataset_src_parquet"
  dataset_ok=0
else
  cp -f "$dataset_src_parquet" "$dataset_cache_parquet"
  cp -f "$dataset_cache_parquet" "$dataset_out_parquet"

  # Create a 1-row parquet to enforce steps=1/batch_size=1 for inference/training.
  if ! "$resolved_python" -c 'import pyarrow' >/dev/null 2>&1; then
    echo "[prepare] ERROR: pyarrow is required to slice the sample parquet (missing dependency)."
    dataset_ok=0
    failure_category="deps"
  else
    echo "[prepare] Creating 1-row parquet: $dataset_out_parquet_1row"
    set +e
    "$resolved_python" - <<'PY' "$dataset_out_parquet" "$dataset_out_parquet_1row"
import sys
import pyarrow.parquet as pq
import pyarrow as pa

src = sys.argv[1]
dst = sys.argv[2]
t = pq.read_table(src)
if t.num_rows < 1:
    raise RuntimeError(f"Parquet has no rows: {src}")
first = t.slice(0, 1)
pq.write_table(first, dst)
PY
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
      echo "[prepare] ERROR: failed to create 1-row parquet (rc=$rc)."
      dataset_ok=0
      failure_category="data"
    fi
  fi

  if [[ "$dataset_ok" -eq 1 ]]; then
    echo "$dataset_out_parquet_1row" > "$dataset_list_1row"

    dataset_sha256="$("$resolved_python" - <<'PY' "$dataset_out_parquet_1row"
import hashlib, sys
p=sys.argv[1]
h=hashlib.sha256()
with open(p,'rb') as f:
  for chunk in iter(lambda: f.read(1024*1024), b''):
    h.update(chunk)
print(h.hexdigest())
PY
    )"
  fi
fi

model_name="InspireMusic-Base"
hf_repo_id="${HF_REPO_ID:-FunAudioLLM/InspireMusic-Base}"
model_cache_dir="$cache_root/model/huggingface/$model_name"
model_out_dir="$model_dir/$model_name"
mkdir -p "$cache_root/model/huggingface"

export HF_HOME="$cache_root/hf_home"
export HF_HUB_DISABLE_TELEMETRY=1

model_source="huggingface:$hf_repo_id"
model_version="main"

echo "[prepare] Model repo: $hf_repo_id"
echo "[prepare] Model cache dir: $model_cache_dir"

expected_model_files=(
  "$model_cache_dir/llm.pt"
  "$model_cache_dir/music_tokenizer/config.json"
  "$model_cache_dir/music_tokenizer/model.pt"
  "$model_cache_dir/wavtokenizer/config.yaml"
  "$model_cache_dir/wavtokenizer/model.pt"
)

have_model=1
for f in "${expected_model_files[@]}"; do
  if [[ ! -f "$f" ]]; then
    have_model=0
    break
  fi
done

download_log=""
if [[ "$have_model" -eq 0 ]]; then
  echo "[prepare] Model not present in cache; attempting download via huggingface_hub.snapshot_download ..."
  set +e
  download_log="$(
    "$resolved_python" - <<'PY' "$hf_repo_id" "$model_cache_dir" 2>&1
import os, sys
from pathlib import Path

repo_id = sys.argv[1]
local_dir = Path(sys.argv[2])
local_dir.mkdir(parents=True, exist_ok=True)

try:
    from huggingface_hub import snapshot_download
except Exception as e:
    raise SystemExit(f"huggingface_hub not available: {type(e).__name__}: {e}")

path = snapshot_download(repo_id=repo_id, local_dir=str(local_dir), local_dir_use_symlinks=False)
print(path)
PY
  )"
  dl_rc=$?
  set -e
  if [[ $dl_rc -ne 0 ]]; then
    echo "[prepare] Download failed (rc=$dl_rc)."
    echo "$download_log"
  fi
else
  echo "[prepare] Reusing cached model (expected files present)."
fi

# Verify model after attempted download (or reuse).
have_model=1
missing_model_file=""
for f in "${expected_model_files[@]}"; do
  if [[ ! -f "$f" ]]; then
    have_model=0
    missing_model_file="$f"
    break
  fi
done

status="success"
exit_code=0
decision_reason="Dataset: repo-provided sample parquet (1-row derived). Model: HuggingFace ${hf_repo_id} into benchmark_assets/cache/, linked to benchmark_assets/model/."

if [[ "$dataset_ok" -eq 0 ]]; then
  status="failure"
  exit_code=1
  if [[ "$failure_category" == "unknown" ]]; then
    failure_category="data"
  fi
fi

if [[ "$have_model" -eq 0 ]]; then
  status="failure"
  exit_code=1
  if [[ "$dataset_ok" -eq 1 ]]; then
    if echo "$download_log" | grep -qiE "(401|403|auth|token)" >/dev/null 2>&1; then
      failure_category="auth_required"
    elif echo "$download_log" | grep -qiE "(connection|timed out|Temporary failure|Name or service not known)" >/dev/null 2>&1; then
      failure_category="download_failed"
    elif echo "$download_log" | grep -qiE "huggingface_hub not available" >/dev/null 2>&1; then
      failure_category="deps"
    else
      # Download logs may indicate success, but expected artifacts missing -> model error by spec.
      failure_category="model"
    fi
  fi
  echo "[prepare] ERROR: expected model file missing: $missing_model_file"
fi

if [[ "$have_model" -eq 1 ]]; then
  # Link or copy into benchmark_assets/model/
  if [[ -e "$model_out_dir" ]] && [[ ! -L "$model_out_dir" ]]; then
    echo "[prepare] model_out_dir exists as a real directory; reusing: $model_out_dir"
  elif [[ -L "$model_out_dir" ]]; then
    echo "[prepare] model_out_dir is a symlink; reusing: $model_out_dir"
  else
    echo "[prepare] Linking model cache -> $model_out_dir"
    ln -s "$model_cache_dir" "$model_out_dir" 2>/dev/null || {
      echo "[prepare] Symlink failed; copying (may be large)."
      cp -a "$model_cache_dir" "$model_out_dir"
    }
  fi

  model_sha256="$("$resolved_python" - <<'PY' "$model_cache_dir/llm.pt"
import hashlib, sys
p=sys.argv[1]
h=hashlib.sha256()
with open(p,'rb') as f:
  for chunk in iter(lambda: f.read(1024*1024), b''):
    h.update(chunk)
print(h.hexdigest())
PY
  )"
fi

# Generate a benchmark config derived from the example, with paths pointing to benchmark_assets.
bench_config_path="$model_out_dir/inspiremusic_benchmark.yaml"
if [[ "$have_model" -eq 1 ]]; then
  echo "[prepare] Writing benchmark config: $bench_config_path"
  set +e
  "$resolved_python" - <<'PY' \
    "$repo_root/examples/music_generation/conf/inspiremusic.yaml" \
    "$bench_config_path" \
    "$model_out_dir" \
    "$model_out_dir/music_tokenizer"
import re, sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
base = sys.argv[3].rstrip("/") + "/"
gen = sys.argv[4].rstrip("/")

text = src.read_text(encoding="utf-8")

def repl_line(key: str, value: str) -> None:
    global text
    # Match: key: '...'
    text = re.sub(rf"^{re.escape(key)}:\s*'[^']*'\s*$", f"{key}: '{value}'", text, flags=re.M)

repl_line("basemodel_path", base)
repl_line("generator_path", gen)

# Minimize training runtime/output for benchmark:
text = re.sub(r"^\s*max_epoch:\s*\d+\s*$", "    max_epoch: 1", text, flags=re.M)
text = re.sub(r"^\s*accum_grad:\s*\d+\s*$", "    accum_grad: 1", text, flags=re.M)
text = re.sub(r"^\s*log_interval:\s*\d+\s*$", "    log_interval: 1", text, flags=re.M)
text = re.sub(r"^\s*save_per_step:\s*\d+\s*$", "    save_per_step: 0", text, flags=re.M)

# Make the sample parquet pass filters if config values are strict:
text = re.sub(r"^\s*min_acoustic_length:\s*\d+\s*$", "    min_acoustic_length: 1", text, flags=re.M)
text = re.sub(r"^\s*min_length:\s*\d+\s*$", "    min_length: 1", text, flags=re.M)

dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(text, encoding="utf-8")
PY
  cfg_rc=$?
  set -e
  if [[ $cfg_rc -ne 0 ]]; then
    echo "[prepare] ERROR: failed to write benchmark config (rc=$cfg_rc)."
    status="failure"
    exit_code=1
    failure_category="runtime"
  fi
fi

"$util_python" - <<'PY' \
  "$results_path" "$status" "$exit_code" "$failure_category" "$framework" "$timeout_sec" \
  "$python_exec" "$python_version" "$git_commit" "$report_path" "$python_resolution" "$decision_reason" "$HF_HOME" \
  "$dataset_out_dir" "$dataset_source" "$dataset_version" "$dataset_sha256" \
  "$model_out_dir" "$model_source" "$model_version" "$model_sha256" \
  "$dataset_list_1row" "$bench_config_path"
import json, sys
from pathlib import Path

(
  results_path,
  status, exit_code, failure_category, framework, timeout_sec,
  python_exec, python_version, git_commit, report_path, python_resolution, decision_reason, hf_home,
  dataset_path, dataset_source, dataset_version, dataset_sha256,
  model_path, model_source, model_version, model_sha256,
  dataset_list_1row, bench_config_path,
) = sys.argv[1:]

payload = {
  "status": status,
  "skip_reason": "unknown",
  "exit_code": int(exit_code),
  "stage": "prepare",
  "task": "download",
  "command": "bash benchmark_scripts/prepare_assets.sh",
  "timeout_sec": int(timeout_sec),
  "framework": framework,
  "assets": {
    "dataset": {"path": dataset_path, "source": dataset_source, "version": dataset_version, "sha256": dataset_sha256},
    "model": {"path": model_path, "source": model_source, "version": model_version, "sha256": model_sha256},
  },
  "meta": {
    "python": python_exec,
    "python_version": python_version,
    "git_commit": git_commit,
    "env_vars": {"HF_HOME": hf_home},
    "decision_reason": decision_reason,
    "report_path": report_path,
    "python_resolution": python_resolution,
    "dataset_list_1row": dataset_list_1row,
    "benchmark_config_path": bench_config_path,
  },
  "failure_category": failure_category if int(exit_code) != 0 else "unknown",
  "error_excerpt": "",
}

Path(results_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY

if [[ "$exit_code" -ne 0 ]]; then
  tail_excerpt="$(tail -n 220 "$log_path" 2>/dev/null || true)"
  "$util_python" - <<'PY' "$results_path" "$tail_excerpt"
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
tail=sys.argv[2]
try:
  d=json.loads(p.read_text(encoding="utf-8"))
except Exception:
  sys.exit(0)
d["error_excerpt"]=tail
p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
PY
fi

exit "$exit_code"
