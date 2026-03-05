#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_DIR="$REPO_ROOT/build_output/multi_gpu"
mkdir -p "$OUT_DIR"
LOG_FILE="$OUT_DIR/log.txt"
RESULTS_JSON="$OUT_DIR/results.json"

# Include benchmark_scripts on PYTHONPATH so `sitecustomize.py` can apply runtime patches
# (without editing repo files), e.g. mapping `--attn_type torch` to a torch-based backend.
export PYTHONPATH="$REPO_ROOT/benchmark_scripts:$REPO_ROOT:${PYTHONPATH:-}"

VISIBLE_DEVICES="${SCIMLOPSBENCH_MULTI_GPU_VISIBLE_DEVICES:-0,1}"
NPROC="${SCIMLOPSBENCH_MULTI_GPU_NPROC:-2}"
MASTER_PORT="${SCIMLOPSBENCH_MULTI_GPU_MASTER_PORT:-29521}"

RING_IMPL_TYPE="${SCIMLOPSBENCH_LONGCTX_RING_IMPL_TYPE:-basic}"
NHEADS="${SCIMLOPSBENCH_LONGCTX_NHEADS:-2}"
GROUP_NUM="${SCIMLOPSBENCH_LONGCTX_GROUP_NUM:-1}"
HEAD_SIZE="${SCIMLOPSBENCH_LONGCTX_HEAD_SIZE:-32}"
SEQ_LEN="${SCIMLOPSBENCH_LONGCTX_SEQ_LEN:-32}"
ULYSSES_DEGREE="${SCIMLOPSBENCH_LONGCTX_ULYSSES_DEGREE:-1}"
NO_CAUSAL="${SCIMLOPSBENCH_LONGCTX_NO_CAUSAL:-1}"
NO_CAUSAL_FLAG=()
if [[ "$NO_CAUSAL" == "1" || "$NO_CAUSAL" == "true" || "$NO_CAUSAL" == "yes" ]]; then
  NO_CAUSAL_FLAG+=(--no_causal)
fi

PY_BIN="$(python3 "$REPO_ROOT/benchmark_scripts/runner.py" resolve-python --requires-python 2>>"$LOG_FILE" || true)"
if [[ -z "$PY_BIN" ]]; then
  {
    echo "[multi_gpu] failed to resolve python from report"
    echo "[multi_gpu] ended_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$LOG_FILE"
  python3 - <<PY >"$RESULTS_JSON"
import json, time, pathlib
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
repo = pathlib.Path(${REPO_ROOT@Q}).resolve()
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "benchmark_scripts/run_multi_gpu_entrypoint.sh",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": str((repo / "benchmark_assets" / "dataset").resolve()), "source": "not_applicable", "version": "unknown", "sha256": ""},
    "model":   {"path": str((repo / "benchmark_assets" / "model").resolve()),   "source": "not_applicable", "version": "unknown", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "Failed to resolve python interpreter via report.json (required for multi-GPU stage).",
    "timestamp_utc": utc(),
  },
  "failure_category": "missing_report",
  "error_excerpt": "Failed to resolve python interpreter via report.json",
}
print(json.dumps(payload, indent=2))
PY
  exit 1
fi

GPU_COUNT="$("$PY_BIN" - <<'PY' 2>>"$LOG_FILE" || true
import torch
print(torch.cuda.device_count())
PY
)"

if [[ -z "$GPU_COUNT" ]]; then
  GPU_COUNT="0"
fi

{
  echo "[multi_gpu] repo_root=$REPO_ROOT"
  echo "[multi_gpu] python=$PY_BIN"
  echo "[multi_gpu] gpu_count=$GPU_COUNT"
  echo "[multi_gpu] visible_devices=$VISIBLE_DEVICES"
  echo "[multi_gpu] nproc_per_node=$NPROC"
  echo "[multi_gpu] master_port=$MASTER_PORT"
} >"$LOG_FILE"

if [[ "$GPU_COUNT" -lt 2 ]]; then
  {
    echo "[multi_gpu] insufficient_hardware: need >=2 GPUs"
    echo "[multi_gpu] ended_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >>"$LOG_FILE"
  GIT_COMMIT=""
  if command -v git >/dev/null 2>&1; then
    GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
  fi
  python3 - <<PY >"$RESULTS_JSON"
import json, time, pathlib
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
repo = pathlib.Path(${REPO_ROOT@Q}).resolve()
payload = {
  "status": "failure",
  "skip_reason": "insufficient_hardware",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": f"{${PY_BIN@Q}} -m torch.distributed.run --nproc_per_node=2 benchmark/benchmark_longctx.py ...",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": str((repo / "benchmark_assets" / "dataset").resolve()), "source": "not_applicable", "version": "unknown", "sha256": ""},
    "model":   {"path": str((repo / "benchmark_assets" / "model").resolve()),   "source": "not_applicable", "version": "unknown", "sha256": ""},
  },
  "meta": {
    "python": ${PY_BIN@Q},
    "git_commit": ${GIT_COMMIT@Q},
    "env_vars": {"CUDA_VISIBLE_DEVICES": ${VISIBLE_DEVICES@Q}},
    "decision_reason": "Multi-GPU stage requires >=2 GPUs; detected fewer than 2.",
    "timestamp_utc": utc(),
    "gpu_count_detected": int(${GPU_COUNT@Q}),
  },
  "failure_category": "runtime",
  "error_excerpt": "insufficient_hardware: need >=2 GPUs",
}
print(json.dumps(payload, indent=2))
PY
  exit 1
fi

python3 "$REPO_ROOT/benchmark_scripts/runner.py" run \
  --stage multi_gpu \
  --task infer \
  --framework pytorch \
  --timeout-sec 1200 \
  --requires-python \
  --env "CUDA_VISIBLE_DEVICES=$VISIBLE_DEVICES" \
  --env "PYTHONPATH=$PYTHONPATH" \
  --env "MASTER_ADDR=127.0.0.1" \
  --env "MASTER_PORT=$MASTER_PORT" \
  --env "NCCL_SHM_DISABLE=1" \
  --env "TORCH_NCCL_ASYNC_ERROR_HANDLING=1" \
  --env "OMP_NUM_THREADS=1" \
  --decision-reason "Native entrypoint: benchmark/benchmark_longctx.py via torch.distributed.run (multi GPU). Run is minimal (batch_size=1, small seq/head dims). Use --no_causal by default for stability across multi-step ring accumulation (override with SCIMLOPSBENCH_LONGCTX_NO_CAUSAL=0). Entrypoint has no CLI for steps=1; user approved running once." \
  --failure-category runtime \
  -- \
  "{python}" -m torch.distributed.run --nproc_per_node="$NPROC" --master_addr=127.0.0.1 --master_port="$MASTER_PORT" \
    benchmark/benchmark_longctx.py \
    --ring_impl_type "$RING_IMPL_TYPE" \
    --nheads "$NHEADS" \
    --group_num "$GROUP_NUM" \
    --head_size "$HEAD_SIZE" \
    --seq_len "$SEQ_LEN" \
    --batch_size 1 \
    --fwd_only \
    --ulysses_degree "$ULYSSES_DEGREE" \
    --attn_type torch \
    "${NO_CAUSAL_FLAG[@]}"
