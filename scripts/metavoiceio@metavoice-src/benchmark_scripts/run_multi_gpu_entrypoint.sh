#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU (>=2) stage using repository-recommended distributed launch.

This repository does not expose an official multi-GPU distributed entrypoint (torchrun/accelerate/deepspeed),
so when >=2 GPUs are present the stage is marked as "skipped" with skip_reason="repo_not_supported".

If <2 GPUs are available, the stage fails with exit code 1.

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Options:
  --python <path>        Override python executable (otherwise resolved from /opt/scimlopsbench/report.json)
  --report-path <path>   Override report path (default: /opt/scimlopsbench/report.json)
  --timeout-sec <n>      Default: 1200 (recorded only)
EOF
}

python_bin=""
report_path=""
timeout_sec="1200"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

stage="multi_gpu"
out_dir="$repo_root/build_output/$stage"
mkdir -p "$out_dir"

prepare_results="$repo_root/build_output/prepare/results.json"

decision_reason_repo="No torchrun/accelerate/deepspeed entrypoints found in README/code; no torch.distributed usage in sources; finetune config notes 'always running finetuning on a single GPU'."

if [[ -z "$python_bin" ]]; then
  python_bin="$(
    RP="$report_path" python3 - <<'PY' 2>/dev/null || true
import json, os, pathlib
rp = os.environ.get("RP") or os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"
p = pathlib.Path(rp)
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print(data.get("python_path",""))
except Exception:
    print("")
PY
  )"
fi

gpu_count="-1"
if [[ -n "$python_bin" ]]; then
  gpu_count="$(
    "$python_bin" - <<'PY' 2>/dev/null || true
try:
    import torch
    print(int(torch.cuda.device_count()))
except Exception:
    print(-1)
PY
  )"
fi

if [[ "$gpu_count" -lt 2 ]]; then
  python3 "$repo_root/benchmark_scripts/runner.py" \
    --stage "$stage" --task infer --framework pytorch --timeout-sec "$timeout_sec" --out-dir "$out_dir" \
    --assets-from "$prepare_results" \
    --no-run --status failure --failure-category runtime \
    --command-str "multi-gpu requires >=2 GPUs; observed gpu_count=$gpu_count" \
    --decision-reason "Insufficient hardware: need >=2 GPUs; observed gpu_count=$gpu_count"
  exit 1
fi

# Repo multi-GPU support not available via official entrypoints -> skipped
python3 "$repo_root/benchmark_scripts/runner.py" \
  --stage "$stage" --task infer --framework pytorch --timeout-sec "$timeout_sec" --out-dir "$out_dir" \
  --assets-from "$prepare_results" \
  --skip --skip-reason repo_not_supported \
  --decision-reason "$decision_reason_repo"
