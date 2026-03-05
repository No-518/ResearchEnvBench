#!/usr/bin/env bash
set -euo pipefail

# FlagGems is an operator library; it documents multi-GPU usage via integration into external
# distributed frameworks (e.g., vLLM) that require code changes outside this repository.
# The repository itself does not ship a native torchrun/accelerate/deepspeed/lightning entrypoint.
# This stage is therefore skipped as repo_not_supported.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/multi_gpu"
mkdir -p "$stage_dir"

pybin="python3"
command -v python3 >/dev/null 2>&1 || pybin="python"

"$pybin" "$repo_root/benchmark_scripts/runner.py" \
  --stage multi_gpu \
  --task infer \
  --framework pytorch \
  --timeout-sec 1200 \
  --assets-from-prepare \
  --skip-reason repo_not_supported \
  --decision-reason "Repo does not provide a distributed launch entrypoint (torchrun/accelerate/deepspeed/lightning). docs/how_to_use_flaggems.md describes multi-node usage by modifying external framework worker code, not via a repo-native CLI." \
  --out-dir "$stage_dir"
