#!/usr/bin/env bash
set -u

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage="multi_gpu"
out_dir="$repo_root/build_output/$stage"
mkdir -p "$out_dir"

export BENCHMARK_ASSETS_DIR="$repo_root/benchmark_assets"
export BENCHMARK_CACHE_DIR="$BENCHMARK_ASSETS_DIR/cache"
export HOME="$BENCHMARK_CACHE_DIR/home"
export XDG_CACHE_HOME="$BENCHMARK_CACHE_DIR/xdg"
export TMPDIR="$BENCHMARK_CACHE_DIR/tmp"
export HF_HOME="$BENCHMARK_CACHE_DIR/huggingface"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export TORCH_HOME="$BENCHMARK_CACHE_DIR/torch"
export PYTHONPATH="$repo_root"
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$TMPDIR" "$HF_HOME" "$HUGGINGFACE_HUB_CACHE" "$TRANSFORMERS_CACHE" "$TORCH_HOME"

export CUDA_VISIBLE_DEVICES="${SCIMLOPSBENCH_MULTI_GPU_CUDA_VISIBLE_DEVICES:-0,1}"
multi_nproc="${SCIMLOPSBENCH_MULTI_GPU_NPROC:-2}"

prepare_results="$repo_root/build_output/prepare/results.json"
dataset_dir="$(python3 - <<PY 2>/dev/null || true
import json, pathlib
p = pathlib.Path(${prepare_results@Q})
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("assets", {}).get("dataset", {}).get("path", "") or "")
except Exception:
    print("")
PY
)"
model_dir="$(python3 - <<PY 2>/dev/null || true
import json, pathlib
p = pathlib.Path(${prepare_results@Q})
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("assets", {}).get("model", {}).get("path", "") or "")
except Exception:
    print("")
PY
)"

model_ckpt="$(python3 - <<PY 2>/dev/null || true
import pathlib
root = pathlib.Path(${model_dir@Q})
if root.exists():
    cands = sorted([p for p in root.rglob('*.pth') if p.is_file()], key=lambda p: (len(p.as_posix()), p.as_posix()))
    print(str(cands[0]) if cands else "")
else:
    print("")
PY
)"

config_path="configs/sana_config/1024ms/Sana_600M_img1024.yaml"
config_src="$repo_root/$config_path"
patched_config="$out_dir/config_patched.yaml"
if python3 "$repo_root/benchmark_scripts/patch_train_config.py" "$config_src" "$patched_config"; then
  config_path="$patched_config"
else
  echo "[$stage] warning: failed to patch config ($config_src); using original $config_path" >&2
fi
work_dir="$out_dir/work_dir"
null_root="$out_dir/null_embed_root"
valid_root="$out_dir/valid_prompt_embed_root"
mkdir -p "$work_dir" "$null_root" "$valid_root"
null_embed_path="$null_root/null_embed_diffusers_gemma-2-2b-it_300token_2304.pth"

master_port="$((RANDOM % 10000 + 20000))"

decision_reason="Entrypoint: train_scripts/train.py via torch.distributed.run (train_scripts/train.sh). Multi-GPU forced via CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES}. DDP launched with nproc_per_node=${multi_nproc}. One-step via --train.early_stop_hours=0 and --train.train_batch_size=1. Script fails early if visible GPU count < 2."

python3 "$repo_root/benchmark_scripts/runner.py" run \
  --stage "$stage" \
  --task "train" \
  --framework "pytorch" \
  --timeout-sec 1200 \
  --requires-python \
  --decision-reason "$decision_reason" \
  --command "{python} -c \"import torch, sys; sys.exit(0 if torch.cuda.device_count()>=2 else 1)\" && {python} benchmark_scripts/ensure_null_embed.py --path='${null_embed_path}' --max-length=300 --hidden-size=2304 && {python} -m torch.distributed.run --nproc_per_node=${multi_nproc} --master_port=${master_port} train_scripts/train.py --config_path='${config_path}' --work_dir='${work_dir}' --report_to=tensorboard --name=scimlops_multi_gpu --data.data_dir='[${dataset_dir}]' --data.type=SanaWebDatasetMS --data.load_vae_feat=true --load_from='${model_ckpt}' --train.train_batch_size=1 --train.num_workers=0 --train.early_stop_hours=0 --train.visualize=false --train.local_save_vis=false --train.null_embed_root='${null_root}' --train.valid_prompt_embed_root='${valid_root}'"
