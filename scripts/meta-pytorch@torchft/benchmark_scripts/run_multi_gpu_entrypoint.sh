#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

python3 benchmark_scripts/runner.py \
  --stage multi_gpu \
  --task train \
  --framework pytorch \
  --requires-python \
  --timeout-sec 1200 \
  --decision-reason "Use README multi-replica-group pattern for train_ddp.py (two torchrun launches, 1 proc each) with a shared lighthouse; enforce batch_size=1 and max_steps=1 via benchmark_scripts/sitecustomize.py; require >=2 visible GPUs." \
  -- bash -lc '
set -euo pipefail
REPO_ROOT="$(pwd)"
OUT_DIR="$REPO_ROOT/build_output/multi_gpu"
WORKDIR="$OUT_DIR/workdir"
mkdir -p "$WORKDIR"

PY="${SCIMLOPSBENCH_RESOLVED_PYTHON:?runner did not set SCIMLOPSBENCH_RESOLVED_PYTHON}"
MANIFEST="$REPO_ROOT/benchmark_assets/manifest.json"
if [[ ! -f "$MANIFEST" ]]; then
  echo "[multi_gpu] missing manifest: $MANIFEST"
  exit 2
fi

read_manifest() {
  local key="$1"
  "$PY" - "$key" <<PY
import json, pathlib, sys
data = json.loads(pathlib.Path("$MANIFEST").read_text(encoding="utf-8"))
assets = data.get("assets", data)
print(assets.get(sys.argv[1], {}).get("path", ""))
PY
}

DATASET_DIR="$(read_manifest dataset)"
MODEL_DIR="$(read_manifest model)"
echo "[multi_gpu] dataset_dir=$DATASET_DIR"
echo "[multi_gpu] model_dir=$MODEL_DIR"

export PYTHONPATH="$REPO_ROOT/benchmark_scripts:$REPO_ROOT:${PYTHONPATH:-}"
export SCIMLOPSBENCH_DATASET_DIR="$DATASET_DIR"
export SCIMLOPSBENCH_MODEL_DIR="$MODEL_DIR"
export SCIMLOPSBENCH_BATCH_SIZE=1
export SCIMLOPSBENCH_NUM_WORKERS=0
export SCIMLOPSBENCH_MAX_STEPS=1
export SCIMLOPSBENCH_EMBEDDING_CAP="${SCIMLOPSBENCH_EMBEDDING_CAP:-1000}"
export SCIMLOPSBENCH_FORCE_CPU=0
export SCIMLOPSBENCH_REQUIRE_CUDA=1
export SCIMLOPSBENCH_REQUIRE_MIN_GPU_COUNT=2

export CUDA_VISIBLE_DEVICES="${SCIMLOPSBENCH_MULTI_GPU_DEVICES:-0,1}"

GPU_COUNT="$("$PY" - <<PY
import torch
print(torch.cuda.device_count())
PY
)"
echo "[multi_gpu] visible_gpu_count=$GPU_COUNT (CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES)"
if [[ "$GPU_COUNT" -lt 2 ]]; then
  echo "[multi_gpu] need >=2 GPUs for this stage"
  exit 1
fi

export NUM_REPLICA_GROUPS=2

LH_ADDR_FILE="$WORKDIR/lighthouse_addr.txt"
LH_LOG="$WORKDIR/lighthouse.log"

cleanup() {
  for pid in ${PIDS:-}; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  if [[ -n "${LH_PID:-}" ]] && kill -0 "$LH_PID" 2>/dev/null; then
    kill "$LH_PID" 2>/dev/null || true
    wait "$LH_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

"$PY" - <<'"'"'PY'"'"' >"$LH_ADDR_FILE" 2>>"$LH_LOG" &
from torchft.coordination import LighthouseServer
import time
lh = LighthouseServer(bind="[::]:0", min_replicas=1, join_timeout_ms=60000)
print(lh.address(), flush=True)
while True:
    time.sleep(3600)
PY
LH_PID=$!

for _ in $(seq 1 100); do
  if [[ -s "$LH_ADDR_FILE" ]]; then break; fi
  sleep 0.1
done
TORCHFT_LIGHTHOUSE="$(head -n 1 "$LH_ADDR_FILE" | tr -d "\r")"
if [[ -z "$TORCHFT_LIGHTHOUSE" ]]; then
  echo "[multi_gpu] failed to start lighthouse"
  exit 3
fi
export TORCHFT_LIGHTHOUSE
echo "[multi_gpu] TORCHFT_LIGHTHOUSE=$TORCHFT_LIGHTHOUSE"

cd "$WORKDIR"

set +e
PIDS=""
(
  export REPLICA_GROUP_ID=0
  "$PY" -m torch.distributed.run --standalone --nnodes 1 --nproc_per_node 1 --master_port 29600 "$REPO_ROOT/train_ddp.py"
) &
P0=$!
PIDS="$PIDS $P0"

(
  export REPLICA_GROUP_ID=1
  "$PY" -m torch.distributed.run --standalone --nnodes 1 --nproc_per_node 1 --master_port 29601 "$REPO_ROOT/train_ddp.py"
) &
P1=$!
PIDS="$PIDS $P1"

wait "$P0"; RC0=$?
wait "$P1"; RC1=$?
set -e

echo "[multi_gpu] replica_group_0_exit=$RC0 replica_group_1_exit=$RC1"
if [[ "$RC0" -ne 0 || "$RC1" -ne 0 ]]; then
  exit 1
fi
exit 0
'
