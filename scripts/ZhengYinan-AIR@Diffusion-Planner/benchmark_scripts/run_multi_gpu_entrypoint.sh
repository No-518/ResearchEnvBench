#!/usr/bin/env bash
set -euo pipefail

# Minimal multi-GPU (DDP) run via repository-recommended launcher:
#   python -m torch.distributed.run ... train_predictor.py

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
cd "$repo_root"

report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"

resolve_python() {
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    echo "${SCIMLOPSBENCH_PYTHON}"
    return 0
  fi
  python3 - <<PY
import json, sys
from pathlib import Path
p = Path(${report_path@Q})
if not p.exists():
  print("")
  sys.exit(1)
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  print("")
  sys.exit(1)
py = data.get("python_path")
if not isinstance(py, str) or not py.strip():
  print("")
  sys.exit(1)
print(py)
PY
}

python_resolved=""
set +e
python_resolved="$(resolve_python)"
py_rc=$?
set -e

if [[ $py_rc -ne 0 || -z "$python_resolved" ]]; then
  # Let runner record the missing_report failure.
  python3 benchmark_scripts/runner.py \
    --stage multi_gpu \
    --task train \
    --framework pytorch \
    --timeout-sec 1200 \
    --decision-reason "Cannot resolve python_path from agent report; multi-GPU stage requires report python." \
    --python-module torch.distributed.run -- \
      --help >/dev/null 2>&1 || true
  exit 1
fi

set +e
gpu_count="$("$python_resolved" -c 'import torch; print(torch.cuda.device_count() if torch.cuda.is_available() else 0)' 2>/dev/null)"
count_rc=$?
set -e

if [[ $count_rc -ne 0 ]]; then
  python3 benchmark_scripts/runner.py \
    --stage multi_gpu \
    --task train \
    --framework pytorch \
    --timeout-sec 1200 \
    --decision-reason "Torch CUDA probe failed under report python; cannot validate multi-GPU availability." -- \
      bash -lc 'echo "Torch CUDA probe failed; cannot determine gpu_count for multi-GPU stage." >&2; exit 1'
  exit 1
fi

gpu_count="${gpu_count:-0}"
if [[ "$gpu_count" -lt 2 ]]; then
  python3 benchmark_scripts/runner.py \
    --stage multi_gpu \
    --task train \
    --framework pytorch \
    --timeout-sec 1200 \
    --decision-reason "Detected gpu_count=${gpu_count} (<2) under report python; need >=2 GPUs for multi-GPU stage." -- \
      bash -lc "echo \"Insufficient hardware: need >=2 GPUs for multi-GPU stage (got ${gpu_count}).\" >&2; exit 1"
  exit 1
fi

# Repo training code divides global batch_size by world_size; use global batch_size=2 to get per-rank batch_size=1.
python3 benchmark_scripts/runner.py \
  --stage multi_gpu \
  --task train \
  --framework pytorch \
  --timeout-sec 1200 \
  --decision-reason "Run DDP via python -m torch.distributed.run (repo torch_run.sh style) for 1 epoch on a 1-sample synthetic dataset. CUDA_VISIBLE_DEVICES=0,1. Global batch_size=2 to achieve per-rank batch_size=1 because train_predictor.py uses batch_size//world_size." \
  --env CUDA_VISIBLE_DEVICES=0,1 \
  --ensure-module timm=timm \
  --ensure-module tensorboard=tensorboard \
  --ensure-module wandb=wandb \
  --python-module torch.distributed.run -- \
    --nnodes 1 \
    --nproc-per-node 2 \
    --standalone \
    train_predictor.py \
      --name scimlopsbench_multi_gpu \
      --save_dir build_output/multi_gpu \
      --train_set benchmark_assets/dataset \
      --train_set_list benchmark_assets/dataset/train_list.json \
      --device cuda \
      --batch_size 2 \
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
