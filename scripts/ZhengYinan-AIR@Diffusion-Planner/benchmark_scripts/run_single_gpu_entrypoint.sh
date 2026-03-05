#!/usr/bin/env bash
set -euo pipefail

# Minimal single-GPU run via repository entrypoint (train_predictor.py).

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
cd "$repo_root"

python3 benchmark_scripts/runner.py \
  --stage single_gpu \
  --task train \
  --framework pytorch \
  --timeout-sec 600 \
  --decision-reason "Run train_predictor.py for 1 epoch on a 1-sample synthetic dataset; force single GPU via CUDA_VISIBLE_DEVICES=0 and --device cuda. Using --ddp false avoids torch.distributed for this single-GPU smoke test." \
  --env CUDA_VISIBLE_DEVICES=0 \
  --ensure-module timm=timm \
  --ensure-module tensorboard=tensorboard \
  --ensure-module wandb=wandb \
  --python-script train_predictor.py -- \
    --name scimlopsbench_single_gpu \
    --save_dir build_output/single_gpu \
    --train_set benchmark_assets/dataset \
    --train_set_list benchmark_assets/dataset/train_list.json \
    --device cuda \
    --ddp false \
    --batch_size 1 \
    --train_epochs 1 \
    --warm_up_epoch 1 \
    --num_workers 0 \
    --use_data_augment false \
    --use_wandb false \
    --notes scimlopsbench \
    --agent_num 2 \
    --predicted_neighbor_num 1 \
    --time_len 2 \
    --future_len 8 \
    --lane_num 2 \
    --lane_len 4 \
    --route_num 1 \
    --route_len 4 \
    --static_objects_num 1 \
    --encoder_depth 1 \
    --decoder_depth 1 \
    --num_heads 2 \
    --hidden_dim 64
