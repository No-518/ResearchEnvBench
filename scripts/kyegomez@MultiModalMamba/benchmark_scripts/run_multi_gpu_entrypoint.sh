#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step multi-GPU (single-node) inference via torch.distributed.run on the repository entrypoint.

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Requirements implemented:
  - Detect GPU count < 2 -> exit 1
  - Force CUDA_VISIBLE_DEVICES=0,1 by default (override with --visible-devices)
  - Use a repository entrypoint script (example.py or model_example.py)
  - Distributed launch via python -m torch.distributed.run

Optional:
  --repo <path>
  --report-path <path>     Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --python <path>          Explicit python executable (highest priority)
  --timeout-sec <n>        Default: 1200
  --visible-devices <ids>  Default: 0,1
  --nproc-per-node <n>     Default: 2
EOF
}

repo_root=""
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_bin=""
timeout_sec="1200"
visible_devices="0,1"
nproc_per_node="2"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo_root="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    --visible-devices) visible_devices="${2:-}"; shift 2 ;;
    --nproc-per-node) nproc_per_node="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$repo_root" ]]; then
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
cd "$repo_root"

out_dir="$repo_root/build_output/multi_gpu"
mkdir -p "$out_dir"
log_file="$out_dir/log.txt"
results_file="$out_dir/results.json"

json_py="$(command -v python3 || command -v python || true)"
if [[ -z "$json_py" ]]; then
  echo "python not found on PATH; cannot run multi-GPU stage." >&2
  exit 1
fi

entrypoint=""
if [[ -f "example.py" ]]; then
  entrypoint="example.py"
elif [[ -f "model_example.py" ]]; then
  entrypoint="model_example.py"
fi

gpu_count=""
if [[ -f "build_output/cuda/results.json" ]]; then
  gpu_count="$("$json_py" - <<'PY' || true
import json
from pathlib import Path
p = Path("build_output/cuda/results.json")
try:
  d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print("")
  raise SystemExit(0)
obs = d.get("observed") if isinstance(d, dict) else None
if isinstance(obs, dict) and isinstance(obs.get("gpu_count"), int):
  print(obs["gpu_count"])
else:
  print("")
PY
)"
fi

if [[ -z "$gpu_count" ]]; then
  # Fallback: use resolved python to check torch.cuda.device_count().
  resolved_py="$("$json_py" benchmark_scripts/runner.py --stage multi_gpu --task infer --report-path "$report_path" ${python_bin:+--python "$python_bin"} --print-python || true)"
  if [[ -n "$resolved_py" ]]; then
    gpu_count="$("$resolved_py" -c 'import torch; print(torch.cuda.device_count() if torch.cuda.is_available() else 0)' 2>/dev/null || true)"
  fi
fi

if [[ -z "$gpu_count" ]]; then
  gpu_count="0"
fi

if [[ "$gpu_count" -lt 2 ]]; then
  : > "$log_file"
  {
    echo "[multi_gpu] Need >=2 GPUs, observed gpu_count=$gpu_count"
    echo "[multi_gpu] Not launching distributed run."
  } | tee -a "$log_file" >/dev/null

  assets_json='{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}}'
  if [[ -f "build_output/prepare/results.json" ]]; then
    assets_json="$("$json_py" - <<'PY' || true
import json
from pathlib import Path
p = Path("build_output/prepare/results.json")
try:
  d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print('{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}}')
  raise SystemExit(0)
print(json.dumps(d.get("assets", {})))
PY
)"
  fi

  "$json_py" - <<PY
import json, time, subprocess, os
from pathlib import Path

out = Path(r"""$results_file""")
assets = json.loads(r'''$assets_json''') if r'''$assets_json''' else {}
if not isinstance(assets, dict) or "dataset" not in assets or "model" not in assets:
  assets = {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  }
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "python -m torch.distributed.run --nproc-per-node=${nproc_per_node} benchmark_scripts/entrypoint_wrapper.py --entrypoint <repo_entrypoint> --device cuda",
  "timeout_sec": int(${timeout_sec}),
  "framework": "pytorch",
  "assets": assets if isinstance(assets, dict) else {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {"CUDA_VISIBLE_DEVICES": "${visible_devices}"},
    "decision_reason": "Hardware insufficient for multi-GPU stage (need >=2 GPUs).",
    "observed_gpu_count": int(${gpu_count}),
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  },
  "failure_category": "runtime",
  "error_excerpt": f"Need >=2 GPUs, observed gpu_count={int(${gpu_count})}",
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY
  exit 1
fi

runner_args=(
  --stage multi_gpu
  --task infer
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --report-path "$report_path"
  --assets-from "build_output/prepare/results.json"
  --decision-reason "Launching torch.distributed.run with nproc_per_node=$nproc_per_node over repo entrypoint ($entrypoint) with per-rank CUDA device selection."
  --env "CUDA_VISIBLE_DEVICES=$visible_devices"
  --env "SCIMLOPSBENCH_DATASET_DIR=$repo_root/benchmark_assets/dataset"
  --env "SCIMLOPSBENCH_MODEL_DIR=$repo_root/benchmark_assets/model"
)
if [[ -n "$python_bin" ]]; then
  runner_args+=(--python "$python_bin")
fi

if [[ -z "$entrypoint" ]]; then
  python benchmark_scripts/runner.py "${runner_args[@]}" \
    --failure-category entrypoint_not_found \
    --decision-reason "No supported repo entrypoint found (expected example.py or model_example.py); cannot run multi-GPU." \
    -- bash -lc 'echo "entrypoint_not_found: expected example.py or model_example.py" >&2; exit 2'
  exit 1
fi

python benchmark_scripts/runner.py "${runner_args[@]}" -- \
  "{python}" -m torch.distributed.run --standalone --nnodes 1 --nproc-per-node "$nproc_per_node" \
  benchmark_scripts/entrypoint_wrapper.py --entrypoint "$entrypoint" --device cuda
