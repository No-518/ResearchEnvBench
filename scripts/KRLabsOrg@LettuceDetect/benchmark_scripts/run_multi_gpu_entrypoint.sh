#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

stage="multi_gpu"
prepare_results="$REPO_ROOT/build_output/prepare/results.json"

# Reliable evidence in this repo:
# - No torch.distributed / DDP usage anywhere
# - scripts/train.py argparse does not accept --local_rank/--local-rank (torchrun injects it)
decision_reason="Repo has no distributed training implementation (no torch.distributed/DDP code; no accelerate/deepspeed integration; scripts/train.py argparse lacks local-rank), so multi-GPU distributed execution is not supported."

python "$REPO_ROOT/benchmark_scripts/runner.py" \
  --stage "$stage" --task train --framework pytorch \
  --assets-from "$prepare_results" \
  --skip --skip-reason repo_not_supported \
  --decision-reason "$decision_reason" \
  --shell -- -- "echo 'skipped: repo_not_supported'; exit 0"

exit 0

