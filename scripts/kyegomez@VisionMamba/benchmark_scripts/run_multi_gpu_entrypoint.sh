#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/multi_gpu"
assets_manifest="$repo_root/benchmark_assets/assets.json"
mkdir -p "$stage_dir"

python3_bin="$(command -v python3 || command -v python)"

if [[ ! -f "$assets_manifest" ]]; then
  "$python3_bin" "$repo_root/benchmark_scripts/runner.py" \
    --stage multi_gpu \
    --task infer \
    --out-dir "$stage_dir" \
    --timeout-sec 1200 \
    --framework pytorch \
    --failure-category data \
    --decision-reason "Missing benchmark_assets/assets.json; run prepare_assets.sh first." \
    -- bash -lc "echo 'Missing assets manifest: $assets_manifest' >&2; exit 1"
  exit 1
fi

devices="${SCIMLOPSBENCH_MULTI_GPU_DEVICES:-0,1}"
IFS=',' read -r -a dev_arr <<<"$devices"
nproc="${#dev_arr[@]}"

worker_py="$stage_dir/torchrun_worker.py"
cat >"$worker_py" <<'PY'
import os
import sys
import runpy
from pathlib import Path

import torch

local_rank = int(os.environ.get("LOCAL_RANK", "0"))

repo_root = Path(__file__).resolve().parents[2]
os.chdir(str(repo_root))
sys.path.insert(0, str(repo_root))

if torch.cuda.is_available():
    try:
        torch.cuda.set_device(local_rank)
    except Exception:
        pass

try:
    if hasattr(torch, "set_default_device"):
        torch.set_default_device("cuda")
except Exception:
    pass

# Patch the repo example to run with the current implementation.
try:
    import vision_mamba.model as vm

    _orig_init = vm.VisionEncoderMambaBlock.__init__

    def _patched_init(self, dim, dt_rank, dim_inner, d_state, *args, **kwargs):
        kwargs.pop("heads", None)
        return _orig_init(self, dim=dim, dt_rank=dt_rank, dim_inner=dim_inner, d_state=d_state)

    vm.VisionEncoderMambaBlock.__init__ = _patched_init

    from einops import rearrange

    def _process_direction(self, x, conv1d, ssm):
        x = rearrange(x, "b s d -> b d s")
        x = self.softplus(conv1d(x))
        x = rearrange(x, "b d s -> b s d")
        x = ssm(x)
        return x

    vm.VisionEncoderMambaBlock.process_direction = _process_direction
except Exception:
    pass

runpy.run_path("example.py", run_name="__main__")
PY

impl_py="$stage_dir/multi_gpu_impl.py"
cat >"$impl_py" <<'PY'
import os
import subprocess
import sys

import torch

nproc = int(os.environ.get("NPROC", "2"))
worker = os.environ.get("WORKER", "")

if not torch.cuda.is_available():
    print("[multi_gpu] CUDA not available")
    raise SystemExit(1)

gpu_count = int(torch.cuda.device_count())
print(f"[multi_gpu] visible_gpu_count={gpu_count}")
if gpu_count < 2:
    print("[multi_gpu] need >= 2 GPUs for this stage")
    raise SystemExit(1)

if not worker:
    print("[multi_gpu] WORKER path missing")
    raise SystemExit(1)

cmd = [
    sys.executable,
    "-m",
    "torch.distributed.run",
    "--standalone",
    "--nproc_per_node",
    str(nproc),
    worker,
]
print("[multi_gpu] launch:", " ".join(cmd))
rc = subprocess.call(cmd)
raise SystemExit(rc)
PY

"$python3_bin" "$repo_root/benchmark_scripts/runner.py" \
  --stage multi_gpu \
  --task infer \
  --out-dir "$stage_dir" \
  --timeout-sec 1200 \
  --framework pytorch \
  --requires-python \
  --assets-json "$assets_manifest" \
  --decision-reason "Launch multi-process run with torch.distributed.run (torchrun) using CUDA_VISIBLE_DEVICES=${devices}; each rank sets its local CUDA device and executes example.py (one forward pass)." \
  --env CUDA_VISIBLE_DEVICES="$devices" \
  --env NPROC="$nproc" \
  --env WORKER="$worker_py" \
  --python-script "$impl_py"
