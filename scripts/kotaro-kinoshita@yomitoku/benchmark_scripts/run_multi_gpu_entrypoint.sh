#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU distributed evaluation (if supported by repo entrypoints).

For this repository, multi-GPU distributed execution is not exposed by the CLI entrypoint.
This stage will be marked as skipped (repo_not_supported) with reviewable evidence in the log.

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json
EOF
}

timeout_sec="${SCIMLOPSBENCH_MULTI_GPU_TIMEOUT_SEC:-1200}"
report_path=""
python_bin=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OUT_DIR="$REPO_ROOT/build_output/multi_gpu"
mkdir -p "$OUT_DIR"
LOG_PATH="$OUT_DIR/log.txt"
: >"$LOG_PATH"
exec > >(tee -a "$LOG_PATH") 2>&1

echo "[multi_gpu] Evidence search (docs + src) for distributed/multi-GPU support:"
if command -v rg >/dev/null 2>&1; then
  rg -n "torch\\.distributed|DistributedDataParallel|\\bddp\\b|torchrun|accelerate launch|deepspeed" -S README.md README_EN.md docs src || true
else
  grep -RInE "torch\\.distributed|DistributedDataParallel|\\bddp\\b|torchrun|accelerate launch|deepspeed" README.md README_EN.md docs src 2>/dev/null || true
fi

decision_reason="No repository-provided multi-GPU/distributed execution entrypoint detected (no torch.distributed usage and no documented torchrun/accelerate/deepspeed launch). Marking multi_gpu as skipped per repo_not_supported."

runner_args=(
  run
  --stage multi_gpu
  --task infer
  --out-dir build_output/multi_gpu
  --timeout-sec "$timeout_sec"
  --framework pytorch
  --assets-from build_output/prepare/results.json
  --decision-reason "$decision_reason"
  --skip-reason repo_not_supported
)

if [[ -n "$python_bin" ]]; then
  runner_args+=(--python "$python_bin")
fi
if [[ -n "$report_path" ]]; then
  runner_args+=(--report-path "$report_path")
fi

python "$REPO_ROOT/benchmark_scripts/runner.py" "${runner_args[@]}" -- \
  echo "skipped: repo_not_supported"
