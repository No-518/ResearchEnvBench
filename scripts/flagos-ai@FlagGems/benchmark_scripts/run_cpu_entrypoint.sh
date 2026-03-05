#!/usr/bin/env bash
set -euo pipefail

# FlagGems requires an accelerator device at import time (see src/flag_gems/runtime/backend/device.py),
# so CPU-only execution is not supported by design. This stage is therefore skipped.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/cpu"
mkdir -p "$stage_dir"

pybin="python3"
command -v python3 >/dev/null 2>&1 || pybin="python"

"$pybin" "$repo_root/benchmark_scripts/runner.py" \
  --stage cpu \
  --task infer \
  --framework pytorch \
  --timeout-sec 600 \
  --assets-from-prepare \
  --skip-reason repo_not_supported \
  --decision-reason "FlagGems import requires a detected accelerator device; no CPU fallback (src/flag_gems/runtime/backend/device.py raises device_not_found when no device is detected)." \
  --out-dir "$stage_dir"
