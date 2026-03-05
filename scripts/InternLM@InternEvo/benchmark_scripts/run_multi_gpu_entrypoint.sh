#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU (2-process, 1 step) training via the repository native entrypoint.

Entrypoint:
  train.py (launched via: python -m torch.distributed.run)

Defaults:
  CUDA_VISIBLE_DEVICES=0,1
  nproc_per_node=2

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json
  build_output/multi_gpu/minimal_config.py

Options:
  --devices <csv>         Default: 0,1 (overrides CUDA_VISIBLE_DEVICES)
  --nproc-per-node <n>    Default: 2
  --master-port <port>    Default: 29512
  --python <path>         Override python interpreter used for torchrun
  --report-path <path>    Override report path
EOF
}

devices="${SCIMLOPSBENCH_MULTI_GPU_DEVICES:-0,1}"
nproc_per_node="${SCIMLOPSBENCH_MULTI_GPU_NPROC:-2}"
master_port="${SCIMLOPSBENCH_MULTI_GPU_PORT:-29512}"
python_override=""
report_path=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --devices) devices="${2:-}"; shift 2 ;;
    --nproc-per-node) nproc_per_node="${2:-}"; shift 2 ;;
    --master-port) master_port="${2:-}"; shift 2 ;;
    --python) python_override="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

out_dir="$REPO_ROOT/build_output/multi_gpu"
mkdir -p "$out_dir"
results_json="$out_dir/results.json"
log_txt="$out_dir/log.txt"
assets_json="$out_dir/assets.json"
config_py="$out_dir/minimal_config.py"
prepare_results="$REPO_ROOT/build_output/prepare/results.json"

resolve_python() {
  local sys_py="${PYTHON:-python}"
  local args=(--stage multi_gpu --task train --out-dir "$out_dir" --print-python)
  if [[ -n "$python_override" ]]; then
    args+=(--python "$python_override")
  fi
  if [[ -n "$report_path" ]]; then
    args+=(--report-path "$report_path")
  fi
  "$sys_py" "$REPO_ROOT/benchmark_scripts/runner.py" "${args[@]}"
}

write_failure_json() {
  local failure_category="$1"
  local skip_reason="$2"
  local message="$3"
  local git_commit
  git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
  : >"$log_txt"
  echo "$message" | tee -a "$log_txt" >/dev/null
  local error_excerpt
  error_excerpt="$(tail -n 200 "$log_txt" || true)"
  python - <<PY
import json, os
out = {
  "status": "failure",
  "skip_reason": ${skip_reason@Q},
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "train",
  "command": "",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": {
    "python": "",
    "git_commit": ${git_commit@Q},
    "env_vars": {k: os.environ.get(k, "") for k in ["SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","CUDA_VISIBLE_DEVICES"] if os.environ.get(k) is not None},
    "decision_reason": "Requires >=2 visible CUDA devices and prepared assets.",
  },
  "failure_category": ${failure_category@Q},
  "error_excerpt": ${error_excerpt@Q},
}
open(${results_json@Q}, "w", encoding="utf-8").write(json.dumps(out, indent=2, ensure_ascii=False) + "\\n")
PY
  exit 1
}

if [[ ! -f "$prepare_results" ]]; then
  write_failure_json "data" "unknown" "Missing $prepare_results; run prepare_assets.sh first."
fi

dataset_dir="$(python - <<PY
import json, pathlib
obj=json.loads(pathlib.Path(${prepare_results@Q}).read_text(encoding="utf-8"))
print(obj.get("assets",{}).get("dataset",{}).get("path",""))
PY
)"
model_path="$(python - <<PY
import json, pathlib
obj=json.loads(pathlib.Path(${prepare_results@Q}).read_text(encoding="utf-8"))
print(obj.get("assets",{}).get("model",{}).get("path",""))
PY
)"

train_folder="$dataset_dir/train"
valid_folder="$dataset_dir/valid"
if [[ -z "$dataset_dir" || ! -d "$train_folder" ]]; then
  write_failure_json "data" "unknown" "Prepared dataset directory missing or invalid: $dataset_dir"
fi

python_bin="$(resolve_python 2>/dev/null || true)"
if [[ -z "$python_bin" ]]; then
  write_failure_json "missing_report" "unknown" "Failed to resolve python (missing/invalid report and no --python override)."
fi

export HF_HOME="$REPO_ROOT/benchmark_assets/cache/hf"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export HF_DATASETS_CACHE="$HF_HOME/datasets"
export TORCH_HOME="$REPO_ROOT/benchmark_assets/cache/torch"
export PIP_CACHE_DIR="$REPO_ROOT/benchmark_assets/cache/pip"
mkdir -p "$HF_HOME" "$TRANSFORMERS_CACHE" "$HF_DATASETS_CACHE" "$TORCH_HOME" "$PIP_CACHE_DIR"

export CUDA_VISIBLE_DEVICES="$devices"
export TOKENIZERS_PARALLELISM="false"
export tensorboard_folder="$out_dir/tensorboard"
mkdir -p "$tensorboard_folder"

# Detect actual visible GPU count (after CUDA_VISIBLE_DEVICES is applied)
set +e
probe_out="$("$python_bin" - <<'PY' 2>&1
import torch
print(f"{int(torch.cuda.is_available())} {int(torch.cuda.device_count())}")
PY
)"
probe_rc="$?"
set -e
if [[ "$probe_rc" -ne 0 ]]; then
  write_failure_json "deps" "unknown" "Failed to import torch / query CUDA devices with python='$python_bin': ${probe_out}"
fi
read -r cuda_ok gpu_count_actual <<<"$(echo "$probe_out" | tail -n 1)"
if [[ "${cuda_ok:-0}" != "1" ]]; then
  write_failure_json "runtime" "insufficient_hardware" "CUDA not available for python='$python_bin' (CUDA_VISIBLE_DEVICES='$devices'). Probe: ${probe_out}"
fi
if [[ -z "${gpu_count_actual:-}" || ! "$gpu_count_actual" =~ ^[0-9]+$ ]]; then
  write_failure_json "runtime" "unknown" "Could not parse GPU count from probe output: ${probe_out}"
fi
if [[ "$gpu_count_actual" -lt 2 ]]; then
  write_failure_json "runtime" "insufficient_hardware" "Need >=2 GPUs for multi-GPU run; observed visible_gpu_count=$gpu_count_actual with CUDA_VISIBLE_DEVICES='$devices'."
fi
if [[ "$nproc_per_node" -lt 2 ]]; then
  write_failure_json "args_unknown" "unknown" "multi_gpu requires --nproc-per-node>=2; got nproc_per_node=$nproc_per_node."
fi
if [[ "$nproc_per_node" -gt "$gpu_count_actual" ]]; then
  write_failure_json "runtime" "insufficient_hardware" "nproc_per_node=$nproc_per_node exceeds visible_gpu_count=$gpu_count_actual (CUDA_VISIBLE_DEVICES='$devices')."
fi

cat >"$config_py" <<PY
import os

JOB_NAME = "scimlopsbench_multi_gpu"
DO_ALERT = False

VOCAB_SIZE = 92544
SEQ_LEN = 32
HIDDEN_SIZE = 256
NUM_ATTENTION_HEAD = 8
NUM_KV_ATTENTION_HEAD = 8
MLP_RATIO = 4
NUM_LAYER = 2
MULTIPLE_OF = 128

model_type = "INTERNLM2"

model = dict(
    num_chunks=1,
    checkpoint=False,
    dtype="torch.float16",
    embed_split_hidden=True,
    num_layers=NUM_LAYER,
    hidden_size=HIDDEN_SIZE,
    vocab_size=VOCAB_SIZE,
    embed_grad_scale=1,
    parallel_output=True,
    num_attention_heads=NUM_ATTENTION_HEAD,
    num_kv_attention_heads=NUM_KV_ATTENTION_HEAD,
    mlp_ratio=MLP_RATIO,
    multiple_of=MULTIPLE_OF,
    norm_type="rmsnorm",
    qk_interleaved=False,
    apply_post_layer_norm=False,
    no_bias=True,
    layer_norm_epsilon=1e-5,
    rope_base=10000,
    norm_head=True,
    use_flash_attn=False,
)

parallel = dict(
    zero1=dict(size=1),
    tensor=dict(size=1, mode="mtp"),
    pipeline=dict(size=1, interleaved_overlap=False, mode="1F1B"),
    weight=dict(size=1, overlap=False),
    expert=dict(size=-1, no_tp=False),
    expert_weight=dict(size=1, overlap=False),
)

ckpt = dict(
    enable_save_ckpt=False,
    auto_resume=False,
    load_ckpt_folder=None,
    load_ckpt_info=None,
    checkpoint_every=10**9,
)

data = dict(
    type="tokenized",
    seq_len=SEQ_LEN,
    micro_num=1,
    micro_bsz=1,
    valid_micro_num=1,
    valid_every=0,
    pack_sample_into_one=False,
    total_steps=1,
    skip_batches="",
    rampup_batch_size="",
    min_length=0,
    train_folder=${train_folder@Q},
    valid_folder=${valid_folder@Q},
    use_shm=False,
    empty_cache_and_diag_interval=200,
    diag_outlier_ratio=1.1,
    tokenizer_path=${model_path@Q},
)

loss = dict(label_smoothing=0)

hybrid_zero_optimizer = dict(
    overlap_sync_grad=False,
    overlap_sync_param=False,
    reduce_bucket_size=64 * 1024 * 1024,
    clip_grad_norm=1.0,
)

adam = dict(
    lr=1e-4,
    adam_beta1=0.9,
    adam_beta2=0.95,
    adam_beta2_c=0,
    adam_eps=1e-8,
    weight_decay=0.01,
)

lr_scheduler = dict(
    total_steps=data["total_steps"],
    init_steps=0,
    warmup_ratio=0.0,
    eta_min=1e-5,
    last_epoch=-1,
)

beta2_scheduler = dict(
    init_beta2=adam["adam_beta2"],
    c=adam["adam_beta2_c"],
    cur_iter=-1,
)

enable_tb = True
PY

python - <<PY
import json, pathlib
obj=json.loads(pathlib.Path(${prepare_results@Q}).read_text(encoding="utf-8"))
assets=obj.get("assets",{})
pathlib.Path(${assets_json@Q}).write_text(json.dumps({"assets": assets}, indent=2, ensure_ascii=False) + "\\n", encoding="utf-8")
PY

decision_reason="Use repo-documented distributed entrypoint (README.md: torchrun train.py --launcher torch) scaled down to 2 processes, with a generated minimal config (1 step) and prepared Alpaca tokenized dataset."
runner_args=(--stage multi_gpu --task train --out-dir "$out_dir" --framework pytorch --timeout-sec 1200 --assets-json "$assets_json" --decision-reason "$decision_reason")
if [[ -n "$python_override" ]]; then
  runner_args+=(--python "$python_override")
fi
if [[ -n "$report_path" ]]; then
  runner_args+=(--report-path "$report_path")
fi

cmd="{python} -m torch.distributed.run --nproc_per_node=${nproc_per_node} --master_port=${master_port} train.py --config ${config_py} --launcher torch"
python "$REPO_ROOT/benchmark_scripts/runner.py" "${runner_args[@]}" --command "$cmd"
