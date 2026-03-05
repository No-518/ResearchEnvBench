#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal CPU inference via the repository's native entrypoint.

Outputs:
  build_output/cpu/log.txt
  build_output/cpu/results.json
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYBIN="$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)"
RUNNER="$REPO_ROOT/benchmark_scripts/runner.py"
PREP_RESULTS="$REPO_ROOT/build_output/prepare/results.json"

decision_reason="Open-Sora inference entrypoint on CPU: torchrun(1 proc) + scripts/diffusion/inference.py with configs/diffusion/inference/256px.py; force CPU by CUDA_VISIBLE_DEVICES=\"\"; minimal generation via --num_steps 1 --num_frames 1 --num_samples 1; outputs confined to build_output/cpu/."

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

if [[ ! -f "$PREP_RESULTS" ]]; then
  "$PYBIN" "$RUNNER" run --stage cpu --task infer --framework pytorch \
    --assets-from "$PREP_RESULTS" \
    --decision-reason "$decision_reason" \
    --fail --failure-category "data" \
    --error-message "Missing prepare stage results at $PREP_RESULTS; run prepare_assets.sh first."
  exit 1
fi

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
  "$PYBIN" "$RUNNER" run --stage cpu --task infer --framework pytorch \
    --assets-from "$PREP_RESULTS" \
    --decision-reason "$decision_reason" \
    --fail --failure-category "model" \
    --error-message "Missing required assets: ${missing[*]}"
  exit 1
fi

export CUDA_VISIBLE_DEVICES=""
export PYTHONPATH="$REPO_ROOT"

save_dir="$REPO_ROOT/build_output/cpu/samples"
mkdir -p "$save_dir"

"$PYBIN" "$RUNNER" run --stage cpu --task infer --framework pytorch \
  --timeout-sec 600 \
  --assets-from "$PREP_RESULTS" \
  --decision-reason "$decision_reason" \
  -- __PYTHON__ -m torch.distributed.run --nproc_per_node 1 --standalone \
  scripts/diffusion/inference.py configs/diffusion/inference/256px.py \
  --save-dir "$save_dir" \
  --dataset.data-path "$dataset_path" \
  --num_samples 1 \
  --num_steps 1 \
  --num_frames 1 \
  --batch_size 1 \
  --num_workers 0 \
  --dtype fp32 \
  --model.from_pretrained "$opensora_ckpt" \
  --ae.from_pretrained "$hunyuan_vae" \
  --t5.from_pretrained "$t5_dir" \
  --clip.from_pretrained "$clip_dir"
