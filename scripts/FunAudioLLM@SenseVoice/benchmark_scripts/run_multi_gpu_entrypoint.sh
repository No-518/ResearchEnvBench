#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU DDP finetune using the repository-recommended entrypoint (FunASR train_ds.py via torchrun).

Defaults:
  CUDA_VISIBLE_DEVICES=0,1

If <2 visible GPUs, exits 1 (insufficient hardware).

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Optional:
  --timeout-sec <int>                 Default: 1200
  --python <path>                     Override python (otherwise resolved from report.json)
  --report-path <path>                Default: /opt/scimlopsbench/report.json
  --cuda-visible-devices <csv>        Default: 0,1
EOF
}

timeout_sec=1200
python_bin=""
report_path=""
cuda_visible_devices="${SCIMLOPSBENCH_MULTI_GPU_VISIBLE_DEVICES:-0,1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --cuda-visible-devices) cuda_visible_devices="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="build_output/multi_gpu"
ASSETS_ROOT="benchmark_assets"
CACHE_ROOT="$ASSETS_ROOT/cache"
HOME_DIR="$CACHE_ROOT/home"
XDG_CACHE_HOME="$CACHE_ROOT/xdg_cache"
HF_HOME="$CACHE_ROOT/hf_home"
HF_HUB_CACHE="$CACHE_ROOT/hf_hub"
HF_DATASETS_CACHE="$CACHE_ROOT/hf_datasets"
TRANSFORMERS_CACHE="$CACHE_ROOT/transformers"
TORCH_HOME="$CACHE_ROOT/torch"

mkdir -p "$OUT_DIR" "$CACHE_ROOT" "$HOME_DIR" "$XDG_CACHE_HOME" "$HF_HOME" "$HF_HUB_CACHE" "$HF_DATASETS_CACHE" "$TRANSFORMERS_CACHE" "$TORCH_HOME"

train_jsonl="$ASSETS_ROOT/dataset/train.jsonl"
val_jsonl="$ASSETS_ROOT/dataset/val.jsonl"

runner_py="$(command -v python3 || command -v python)"

cmd="$(cat <<'BASH'
set -euo pipefail

export CUDA_VISIBLE_DEVICES="${BENCH_CUDA_VISIBLE_DEVICES}"

train_jsonl="${BENCH_TRAIN_JSONL}"
val_jsonl="${BENCH_VAL_JSONL}"

if [[ ! -f "$train_jsonl" ]]; then
  echo "data: missing train jsonl: $train_jsonl" >&2
  exit 1
fi
if [[ ! -f "$val_jsonl" ]]; then
  echo "data: missing val jsonl: $val_jsonl" >&2
  exit 1
fi

# GPU precheck
gpu_count="$("$BENCH_PYTHON" - <<'PY'
import os
try:
    import torch
    print(torch.cuda.device_count())
except Exception:
    print(0)
PY
)"
if [[ "${gpu_count}" -lt 2 ]]; then
  echo "runtime: need >=2 GPUs visible for multi-gpu stage, got ${gpu_count} (CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES})" >&2
  exit 1
fi

train_tool="$("$BENCH_PYTHON" - <<'PY'
import glob
import pathlib
import shutil
import sys

def pick(path: str) -> None:
    p = pathlib.Path(path)
    if p.is_file():
        print(str(p.resolve()))
        raise SystemExit(0)

funasr_bin = shutil.which("funasr")
if funasr_bin:
    pick(str(pathlib.Path(funasr_bin).resolve().parent / "train_ds.py"))
    for g in glob.glob(str(pathlib.Path(funasr_bin).resolve().parent / "../lib/python*/site-packages/funasr/bin/train_ds.py")):
        pick(g)

try:
    import funasr  # noqa
    pkg_dir = pathlib.Path(funasr.__file__).resolve().parent
    pick(str(pkg_dir / "bin" / "train_ds.py"))
except Exception:
    pass

print("")
PY
)"

if [[ -z "$train_tool" || ! -f "$train_tool" ]]; then
  echo "entrypoint_not_found: funasr train_ds.py not found (install funasr>=1.1.3)" >&2
  exit 1
fi

gpu_num=$(echo "$CUDA_VISIBLE_DEVICES" | awk -F "," '{print NF}')
master_port="${MASTER_PORT:-29669}"
output_dir="${BENCH_OUTPUT_DIR}"
mkdir -p "$output_dir"

deepspeed_config="${BENCH_DEEPSPEED_CONFIG}"

echo "[multi_gpu] Using train tool: $train_tool"
echo "[multi_gpu] Using CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES} (nproc=${gpu_num})"

"$BENCH_PYTHON" -m torch.distributed.run \
  --nnodes 1 \
  --nproc_per_node "$gpu_num" \
  --node_rank 0 \
  --master_addr 127.0.0.1 \
  --master_port "$master_port" \
  "$train_tool" \
  ++model="${BENCH_MODEL_ID}" \
  ++trust_remote_code=true \
  ++train_data_set_list="${train_jsonl}" \
  ++valid_data_set_list="${val_jsonl}" \
  ++dataset_conf.data_split_num=1 \
  ++dataset_conf.batch_sampler="BatchSampler" \
  ++dataset_conf.batch_size=6000 \
  ++dataset_conf.sort_size=2 \
  ++dataset_conf.batch_type="token" \
  ++dataset_conf.num_workers=0 \
  ++train_conf.max_epoch=1 \
  ++train_conf.log_interval=1 \
  ++train_conf.resume=false \
  ++train_conf.validate_interval=1000000000 \
  ++train_conf.save_checkpoint_interval=1000000000 \
  ++train_conf.keep_nbest_models=1 \
  ++train_conf.avg_nbest_model=1 \
  ++train_conf.use_deepspeed=false \
  ++train_conf.deepspeed_config="${deepspeed_config}" \
  ++optim_conf.lr=0.0002 \
  ++output_dir="${output_dir}"
BASH
)"

exec "$runner_py" benchmark_scripts/runner.py \
  --stage multi_gpu \
  --task train \
  --framework pytorch \
  --timeout-sec "$timeout_sec" \
  --out-dir "$OUT_DIR" \
  --requires-python \
  ${report_path:+--report-path "$report_path"} \
  ${python_bin:+--python "$python_bin"} \
  --decision-reason "Uses repo-provided finetune.sh as evidence for torchrun + FunASR train_ds.py entrypoint; runs 1 epoch on a tiny jsonl dataset prepared in benchmark_assets/dataset." \
  --env "BENCH_REPO_ROOT=$REPO_ROOT" \
  --env "BENCH_OUTPUT_DIR=$OUT_DIR/outputs" \
  --env "BENCH_CUDA_VISIBLE_DEVICES=$cuda_visible_devices" \
  --env "BENCH_TRAIN_JSONL=$train_jsonl" \
  --env "BENCH_VAL_JSONL=$val_jsonl" \
  --env "BENCH_MODEL_ID=iic/SenseVoiceSmall" \
  --env "BENCH_DEEPSPEED_CONFIG=$REPO_ROOT/deepspeed_conf/ds_stage1.json" \
  --env "CUDA_VISIBLE_DEVICES=$cuda_visible_devices" \
  --env "HOME=$HOME_DIR" \
  --env "XDG_CACHE_HOME=$XDG_CACHE_HOME" \
  --env "HF_HOME=$HF_HOME" \
  --env "HF_HUB_CACHE=$HF_HUB_CACHE" \
  --env "HF_DATASETS_CACHE=$HF_DATASETS_CACHE" \
  --env "TRANSFORMERS_CACHE=$TRANSFORMERS_CACHE" \
  --env "TORCH_HOME=$TORCH_HOME" \
  --env "PYTHONDONTWRITEBYTECODE=1" \
  --env "PYTHONUNBUFFERED=1" \
  -- bash -lc "$cmd"

