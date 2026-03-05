#!/usr/bin/env bash
set -euo pipefail

# This repository is GPU-only by design:
# - nanovllm/engine/model_runner.py hard-codes NCCL backend and CUDA devices.
# - nanovllm/layers/attention.py imports CUDA-only flash-attn and uses Triton kernels.
#
# Per benchmark rules, CPU stage is marked as "skipped" with exit code 0 when CPU
# execution is not supported as native functionality.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/build_output/cpu"
ASSETS_JSON="$REPO_ROOT/benchmark_assets/assets.json"

mkdir -p "$OUT_DIR"
LOG_TXT="$OUT_DIR/log.txt"
RESULTS_JSON="$OUT_DIR/results.json"

timestamp_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ" || true)"
git_commit="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"

PY_BOOTSTRAP=""
if command -v python3 >/dev/null 2>&1; then
  PY_BOOTSTRAP="python3"
elif command -v python >/dev/null 2>&1; then
  PY_BOOTSTRAP="python"
else
  echo "ERROR: python/python3 not found in PATH" >&2
  exit 1
fi

cat >"$LOG_TXT" <<EOF
[cpu] skipped: repo_not_supported
[cpu] Evidence:
  - nanovllm/engine/model_runner.py initializes torch.distributed with backend="nccl" and calls torch.cuda APIs unconditionally.
  - nanovllm/layers/attention.py imports flash_attn and uses Triton kernels (CUDA-only).
EOF

"$PY_BOOTSTRAP" - <<PY || true
import json, pathlib

repo_root = pathlib.Path(${REPO_ROOT@Q})
assets_path = pathlib.Path(${ASSETS_JSON@Q})
assets = {
  "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
  "model": {"path": "", "source": "", "version": "", "sha256": ""},
}
if assets_path.exists():
  try:
    data = json.loads(assets_path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
      assets = data
  except Exception:
    pass

payload = {
  "status": "skipped",
  "skip_reason": "repo_not_supported",
  "exit_code": 0,
  "stage": "cpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": assets,
  "meta": {
    "python": "",
    "git_commit": ${git_commit@Q},
    "env_vars": {"CUDA_VISIBLE_DEVICES": ""},
    "decision_reason": "Nano-vLLM hard-codes CUDA/NCCL execution; no CPU fallback is implemented.",
    "timestamp_utc": ${timestamp_utc@Q},
  },
  "failure_category": "not_applicable",
  "error_excerpt": "",
}

pathlib.Path(${RESULTS_JSON@Q}).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY

exit 0
