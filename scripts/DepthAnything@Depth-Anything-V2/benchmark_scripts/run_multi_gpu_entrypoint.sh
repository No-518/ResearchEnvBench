#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/multi_gpu"

decision_reason="No repository-supported distributed multi-GPU inference entrypoint is exposed for metric_depth/run.py (no DDP/torchrun options). The repo's multi-GPU script metric_depth/dist_train.sh targets training and depends on external datasets not prepared by this benchmark, so multi-GPU is marked not_applicable for this inference-focused workflow."

python "$repo_root/benchmark_scripts/runner.py" \
  --stage "multi_gpu" \
  --task "infer" \
  --framework "pytorch" \
  --out-dir "$stage_dir" \
  --timeout-sec 1200 \
  --decision-reason "$decision_reason" \
  --skip \
  --skip-reason "not_applicable"

exit $?
