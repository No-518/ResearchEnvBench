#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 1

out_dir="$repo_root/build_output/single_gpu"
mkdir -p "$out_dir"

assets_json_path="$out_dir/assets.json"
prepare_results="$repo_root/build_output/prepare/results.json"

if [[ -f "$prepare_results" ]]; then
  jq -c '.assets // {"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}}' \
    "$prepare_results" >"$assets_json_path" 2>/dev/null \
    || echo '{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}}' >"$assets_json_path"
else
  echo '{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}}' >"$assets_json_path"
fi

model_name="$(jq -r '.meta.model_name // "Qwen2.5-0.5B"' "$prepare_results" 2>/dev/null || echo "Qwen2.5-0.5B")"
dataset_path="$(jq -r '.assets.dataset.path // empty' "$prepare_results" 2>/dev/null || true)"
model_path="$(jq -r '.assets.model.path // empty' "$prepare_results" 2>/dev/null || true)"

if [[ -z "$dataset_path" || -z "$model_path" ]]; then
  # Let runner produce a structured failure.
  python3 benchmark_scripts/runner.py \
    --stage single_gpu --task infer --out-dir "$out_dir" --framework pytorch \
    --assets-json "$assets_json_path" \
    --decision-reason "Prepare stage did not provide dataset/model paths." \
    --env CUDA_VISIBLE_DEVICES=0 \
    --python-mode -- -c "raise SystemExit('missing dataset/model paths; run prepare_assets.sh first')" \
    || true
  exit 1
fi

dataset_path="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$dataset_path" 2>/dev/null || echo "$dataset_path")"
model_path="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$model_path" 2>/dev/null || echo "$model_path")"

hydra_dir="$out_dir/hydra"
mkdir -p "$hydra_dir"

HF_HOME="$repo_root/benchmark_assets/cache/huggingface"
HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
TRANSFORMERS_CACHE="$HF_HOME/transformers"
TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
py_path="$repo_root"
if [[ -n "${PYTHONPATH:-}" ]]; then
  py_path="$repo_root:$PYTHONPATH"
fi

python3 benchmark_scripts/runner.py \
  --stage single_gpu --task infer --out-dir "$out_dir" --framework pytorch --timeout-sec 600 \
  --assets-json "$assets_json_path" \
  --decision-reason "Run Chitu offline inference benchmark with torch.distributed.run (1 GPU, 1 iter, 1 request) and Hydra output redirected under build_output." \
  --env CUDA_VISIBLE_DEVICES=0 \
  --env PYTHONPATH="$py_path" \
  --env HF_HOME="$HF_HOME" \
  --env HUGGINGFACE_HUB_CACHE="$HUGGINGFACE_HUB_CACHE" \
  --env TRANSFORMERS_CACHE="$TRANSFORMERS_CACHE" \
  --env TORCH_HOME="$TORCH_HOME" \
  --env XDG_CACHE_HOME="$XDG_CACHE_HOME" \
  --python-mode -- \
    -m torch.distributed.run \
    --nproc_per_node=1 \
    --master_port=29500 \
    --module benchmarks.benchmark_offline \
    "hydra.run.dir=$hydra_dir" \
    "hydra.output_subdir=.hydra" \
    "models=$model_name" \
    "models.ckpt_dir=$model_path" \
    "models.tokenizer_path=$model_path" \
    "infer.tp_size=1" \
    "infer.pp_size=1" \
    "infer.dp_size=1" \
    "infer.ep_size=1" \
    "infer.max_reqs=1" \
    "infer.max_seq_len=32" \
    "infer.prefill_chunk_size=1" \
    "infer.use_cuda_graph=False" \
    "infer.attn_type=ref" \
    "benchmark.iters=1" \
    "benchmark.num_reqs_list=[1]" \
    "benchmark.input_len=8" \
    "benchmark.output_len=8" \
    "benchmark.dataset=sharegpt" \
    "benchmark.dataset_path=$dataset_path"

exit $?
