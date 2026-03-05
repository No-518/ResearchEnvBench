#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Include benchmark_scripts on PYTHONPATH so `sitecustomize.py` can apply runtime patches
# (without editing repo files), e.g. mapping `--attn_type torch` to a torch-based backend.
export PYTHONPATH="$REPO_ROOT/benchmark_scripts:$REPO_ROOT:${PYTHONPATH:-}"

MASTER_PORT="${SCIMLOPSBENCH_SINGLE_GPU_MASTER_PORT:-29511}"

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

python3 "$REPO_ROOT/benchmark_scripts/runner.py" run \
  --stage single_gpu \
  --task infer \
  --framework pytorch \
  --timeout-sec 600 \
  --requires-python \
  --env "CUDA_VISIBLE_DEVICES=0" \
  --env "PYTHONPATH=$PYTHONPATH" \
  --env "MASTER_ADDR=127.0.0.1" \
  --env "MASTER_PORT=$MASTER_PORT" \
  --env "NCCL_SHM_DISABLE=1" \
  --env "TORCH_NCCL_ASYNC_ERROR_HANDLING=1" \
  --env "OMP_NUM_THREADS=1" \
  --decision-reason "Native entrypoint: benchmark/benchmark_longctx.py via torch.distributed.run (single GPU). Run is minimal (batch_size=1, small seq/head dims). Use --no_causal by default for stability across backends (override with SCIMLOPSBENCH_LONGCTX_NO_CAUSAL=0). Entrypoint has no CLI for steps=1; user approved running once." \
  --failure-category runtime \
  -- \
  "{python}" -m torch.distributed.run --nproc_per_node=1 --master_addr=127.0.0.1 --master_port="$MASTER_PORT" \
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
