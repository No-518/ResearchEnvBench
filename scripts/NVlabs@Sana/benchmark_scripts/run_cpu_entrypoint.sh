#!/usr/bin/env bash
set -u

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage="cpu"
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

export CUDA_VISIBLE_DEVICES=""
export ACCELERATE_USE_CPU="true"

decision_reason="CPU stage skipped: this repo's training entrypoint is not CPU-safe. Evidence: diffusion/model/nets/sana_blocks.py uses unconditional CUDA tensor moves (e.g., torch.rand(...).cuda()) during training, and train_scripts/train.py calls torch.cuda.synchronize() in the training loop. With CUDA hidden/absent, these raise 'No CUDA GPUs are available'."

python3 "$repo_root/benchmark_scripts/runner.py" run \
  --stage "$stage" \
  --task "train" \
  --framework "pytorch" \
  --timeout-sec 600 \
  --requires-python \
  --skip \
  --skip-reason "repo_not_supported" \
  --decision-reason "$decision_reason" \
  --command "true"

{
  echo "[cpu] skip_reason=repo_not_supported"
  echo "[cpu] evidence_1=diffusion/model/nets/sana_blocks.py uses '.cuda()' in token_drop (CPU-only fails)"
  echo "[cpu] evidence_2=train_scripts/train.py calls torch.cuda.synchronize() (CPU-only fails)"
} >>"$out_dir/log.txt"

exit 0

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

decision_reason="Entrypoint: train_scripts/train.py via torch.distributed.run (train_scripts/train.sh). Config: $config_path. Dataset/model from build_output/prepare/results.json. CPU forced via CUDA_VISIBLE_DEVICES=''. One-step enforced via --train.early_stop_hours=0 and --train.train_batch_size=1."

python3 "$repo_root/benchmark_scripts/runner.py" run \
  --stage "$stage" \
  --task "train" \
  --framework "pytorch" \
  --timeout-sec 600 \
  --requires-python \
  --decision-reason "$decision_reason" \
  --command "{python} benchmark_scripts/ensure_null_embed.py --path='${null_embed_path}' --max-length=300 --hidden-size=2304 && {python} -m torch.distributed.run --nproc_per_node=1 --master_port=${master_port} train_scripts/train.py --config_path='${config_path}' --work_dir='${work_dir}' --report_to=tensorboard --name=scimlops_cpu --data.data_dir='[${dataset_dir}]' --data.type=SanaWebDatasetMS --data.load_vae_feat=true --load_from='${model_ckpt}' --train.train_batch_size=1 --train.num_workers=0 --train.early_stop_hours=0 --train.visualize=false --train.local_save_vis=false --train.null_embed_root='${null_root}' --train.valid_prompt_embed_root='${valid_root}'"
