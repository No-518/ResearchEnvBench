#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU distributed run via a repository entrypoint (if available).

Default behavior for this repository:
  - Mark as skipped (not_applicable), because the documented entrypoint `run.py` does not expose
    any distributed / DDP launch options for relative-depth inference.

To force an actual multi-GPU attempt, provide an explicit command:
  SCIMLOPSBENCH_MULTI_GPU_CMD='torchrun --nproc_per_node=2 ...' bash benchmark_scripts/run_multi_gpu_entrypoint.sh

Environment:
  SCIMLOPSBENCH_MULTI_GPU_CMD                   Command to execute (required to run; otherwise skipped)
  SCIMLOPSBENCH_MULTI_GPU_CUDA_VISIBLE_DEVICES  Default: 0,1

Outputs (always written, even on failure/skipped):
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

sys_py="$(command -v python3 || command -v python || true)"
if [[ -z "$sys_py" ]]; then
  mkdir -p build_output/multi_gpu
  printf '%s\n' "python/python3 not found on PATH" > build_output/multi_gpu/log.txt
  cat > build_output/multi_gpu/results.json <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "python/python3 not found"},
  "failure_category": "deps",
  "error_excerpt": "python/python3 not found on PATH"
}
JSON
  exit 1
fi

prepare_results="build_output/prepare/results.json"
assets_from=()
if [[ -f "$prepare_results" ]]; then
  assets_from=(--assets-from "$prepare_results")
fi

multi_cmd="${SCIMLOPSBENCH_MULTI_GPU_CMD:-}"
if [[ -z "$multi_cmd" ]]; then
  decision_reason="Skipped: no official multi-GPU distributed entrypoint detected for the documented relative-depth inference command (README only describes single-process run.py; run.py has no distributed flags). Set SCIMLOPSBENCH_MULTI_GPU_CMD to override."
  "$sys_py" benchmark_scripts/runner.py \
    --stage multi_gpu \
    --task infer \
    --timeout-sec 1200 \
    --framework pytorch \
    "${assets_from[@]}" \
    --decision-reason "$decision_reason" \
    --skip \
    --skip-reason not_applicable \
    --command-string "skipped"
  exit 0
fi

# Resolve benchmark python to check hardware.
bench_py="${SCIMLOPSBENCH_PYTHON:-}"
if [[ -z "$bench_py" ]]; then
  bench_py="$("$sys_py" benchmark_scripts/runner.py --stage multi_gpu --task infer --print-python 2>>build_output/multi_gpu/log.txt || true)"
fi

if [[ -z "$bench_py" ]]; then
  "$sys_py" benchmark_scripts/runner.py \
    --stage multi_gpu \
    --task infer \
    --timeout-sec 1200 \
    --framework pytorch \
    "${assets_from[@]}" \
    --decision-reason "Failed to resolve benchmark python (missing report?)" \
    --failure-category missing_report \
    --command-string "$multi_cmd" \
    -- bash -lc "echo 'missing report/python'; exit 1"
  exit 1
fi

gpu_info="$("$bench_py" - <<'PY' 2>/dev/null || true
try:
  import torch
  print(int(torch.cuda.is_available()))
  print(int(torch.cuda.device_count()))
except Exception:
  print(0)
  print(0)
PY
)"
cuda_avail="$(printf '%s\n' "$gpu_info" | head -n 1 || true)"
gpu_count="$(printf '%s\n' "$gpu_info" | tail -n 1 || true)"

if [[ "${cuda_avail:-0}" != "1" || "${gpu_count:-0}" -lt 2 ]]; then
  "$sys_py" benchmark_scripts/runner.py \
    --stage multi_gpu \
    --task infer \
    --timeout-sec 1200 \
    --framework pytorch \
    "${assets_from[@]}" \
    --decision-reason "Need >=2 GPUs for multi-GPU run (cuda=$cuda_avail, gpus=$gpu_count)" \
    --failure-category insufficient_hardware \
    --command-string "$multi_cmd" \
    -- bash -lc "echo 'insufficient hardware: need >=2 GPUs'; exit 1"
  exit 1
fi

cuda_visible="${SCIMLOPSBENCH_MULTI_GPU_CUDA_VISIBLE_DEVICES:-0,1}"
decision_reason="Executing user-provided distributed command via SCIMLOPSBENCH_MULTI_GPU_CMD with CUDA_VISIBLE_DEVICES=$cuda_visible."

"$sys_py" benchmark_scripts/runner.py \
  --stage multi_gpu \
  --task infer \
  --timeout-sec 1200 \
  --framework pytorch \
  "${assets_from[@]}" \
  --decision-reason "$decision_reason" \
  --env "CUDA_VISIBLE_DEVICES=$cuda_visible" \
  --command-string "$multi_cmd" \
  -- bash -lc "$multi_cmd"
