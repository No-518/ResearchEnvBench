#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU inference via the repository's native entrypoint.

Defaults:
  CUDA_VISIBLE_DEVICES=0,1
  nproc_per_node = number of visible GPUs (must be >=2)

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Options:
  --gpus <csv>   Example: --gpus 0,1,2,3
EOF
}

gpus="0,1"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gpus) gpus="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYBIN="$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)"
RUNNER="$REPO_ROOT/benchmark_scripts/runner.py"
PREP_RESULTS="$REPO_ROOT/build_output/prepare/results.json"

decision_reason="Open-Sora inference entrypoint on multi-GPU: torchrun(nproc>=2) + scripts/diffusion/inference.py with configs/diffusion/inference/256px_tp.py (official hybrid/tensor-parallel config); force GPUs via CUDA_VISIBLE_DEVICES=${gpus}; minimal generation via --num_steps 1 --num_frames 1 --num_samples 1; outputs confined to build_output/multi_gpu/."

if [[ ! -f "$PREP_RESULTS" ]]; then
  "$PYBIN" "$RUNNER" run --stage multi_gpu --task infer --framework pytorch \
    --assets-from "$PREP_RESULTS" \
    --decision-reason "$decision_reason" \
    --fail --failure-category "data" \
    --error-message "Missing prepare stage results at $PREP_RESULTS; run prepare_assets.sh first."
  exit 1
fi

read_paths() {
  "$PYBIN" - "$PREP_RESULTS" <<'PY'
import json, sys
p = sys.argv[1]
data = json.load(open(p, "r", encoding="utf-8"))
assets = data.get("assets", {}) if isinstance(data, dict) else {}
ds = (assets.get("dataset") or {}).get("path", "")
model = assets.get("model") or {}
comps = model.get("components") or {}
def gp(name, key="path"):
    return (comps.get(name) or {}).get(key, "")
print(ds)
print(gp("opensora_ckpt"))
print(gp("hunyuan_vae"))
print(gp("t5"))
print(gp("clip"))
PY
}

mapfile -t vals < <(read_paths)
dataset_path="${vals[0]:-}"
opensora_ckpt="${vals[1]:-}"
hunyuan_vae="${vals[2]:-}"
t5_dir="${vals[3]:-}"
clip_dir="${vals[4]:-}"

missing=()
[[ -n "$dataset_path" && -f "$dataset_path" ]] || missing+=("dataset:$dataset_path")
[[ -n "$opensora_ckpt" && -f "$opensora_ckpt" ]] || missing+=("opensora_ckpt:$opensora_ckpt")
[[ -n "$hunyuan_vae" && -f "$hunyuan_vae" ]] || missing+=("hunyuan_vae:$hunyuan_vae")
[[ -n "$t5_dir" && -d "$t5_dir" ]] || missing+=("t5_dir:$t5_dir")
[[ -n "$clip_dir" && -d "$clip_dir" ]] || missing+=("clip_dir:$clip_dir")

if [[ "${#missing[@]}" -gt 0 ]]; then
  "$PYBIN" "$RUNNER" run --stage multi_gpu --task infer --framework pytorch \
    --assets-from "$PREP_RESULTS" \
    --decision-reason "$decision_reason" \
    --fail --failure-category "model" \
    --error-message "Missing required assets: ${missing[*]}"
  exit 1
fi

# GPU count check (must have >= 2 real GPUs available)
resolved_python="$("$PYBIN" "$RUNNER" resolve-python 2>/dev/null || true)"
gpu_count=0
if [[ -n "$resolved_python" && -x "$resolved_python" ]]; then
  set +e
  gpu_count="$("$resolved_python" -c 'import torch; print(torch.cuda.device_count())' 2>/dev/null)"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    gpu_count=0
  fi
fi

if [[ "${gpu_count:-0}" -lt 2 ]]; then
  "$PYBIN" "$RUNNER" run --stage multi_gpu --task infer --framework pytorch \
    --assets-from "$PREP_RESULTS" \
    --decision-reason "$decision_reason" \
    --fail --failure-category "runtime" \
    --error-message "Need >=2 GPUs for multi-GPU stage; observed gpu_count=${gpu_count:-0}."
  exit 1
fi

IFS=',' read -r -a gpu_arr <<<"$gpus"
nproc="${#gpu_arr[@]}"
if [[ "$nproc" -lt 2 ]]; then
  "$PYBIN" "$RUNNER" run --stage multi_gpu --task infer --framework pytorch \
    --assets-from "$PREP_RESULTS" \
    --decision-reason "$decision_reason" \
    --fail --failure-category "runtime" \
    --error-message "--gpus must include at least 2 devices; got '$gpus'."
  exit 1
fi

export CUDA_VISIBLE_DEVICES="$gpus"
export PYTHONPATH="$REPO_ROOT"

save_dir="$REPO_ROOT/build_output/multi_gpu/samples"
mkdir -p "$save_dir"

"$PYBIN" "$RUNNER" run --stage multi_gpu --task infer --framework pytorch \
  --timeout-sec 1200 \
  --assets-from "$PREP_RESULTS" \
  --decision-reason "$decision_reason" \
  -- __PYTHON__ -m torch.distributed.run --nproc_per_node "$nproc" --standalone \
  scripts/diffusion/inference.py configs/diffusion/inference/256px_tp.py \
  --save-dir "$save_dir" \
  --dataset.data-path "$dataset_path" \
  --num_samples 1 \
  --num_steps 1 \
  --num_frames 1 \
  --batch_size 1 \
  --num_workers 0 \
  --dtype fp16 \
  --model.from_pretrained "$opensora_ckpt" \
  --ae.from_pretrained "$hunyuan_vae" \
  --t5.from_pretrained "$t5_dir" \
  --clip.from_pretrained "$clip_dir"
