#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

OUT_DIR="build_output/multi_gpu"
PREP_RESULTS="build_output/prepare/results.json"
CUDA_RESULTS="build_output/cuda/results.json"

GPU_IDS="${SCIMLOPSBENCH_MULTI_GPU_IDS:-0,1}"

mkdir -p "${REPO_ROOT}/${OUT_DIR}"
cd "$REPO_ROOT"

RUNNER_PY="$(command -v python3 || true)"
if [[ -z "$RUNNER_PY" ]]; then
  RUNNER_PY="$(command -v python || true)"
fi

if [[ -z "$RUNNER_PY" ]]; then
  mkdir -p "${OUT_DIR}"
  printf '%s\n' "[multi_gpu] ERROR: python3/python not found on PATH" >"${OUT_DIR}/log.txt"
  cat >"${OUT_DIR}/results.json" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "train",
  "command": "benchmark_scripts/run_multi_gpu_entrypoint.sh",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "python3/python not found on PATH",
    "timestamp_utc": ""
  },
  "failure_category": "deps",
  "error_excerpt": "python3/python not found on PATH"
}
JSON
  exit 1
fi

gpu_ids_count="$(printf '%s' "$GPU_IDS" | awk -F',' '{print NF}')"
if [[ "${gpu_ids_count:-0}" -lt 2 ]]; then
	"$RUNNER_PY" benchmark_scripts/runner.py \
	    --stage multi_gpu \
	    --task train \
	    --out-dir "$OUT_DIR" \
	    --timeout-sec 1200 \
	    --framework pytorch \
	    --no-requires-python \
	    --failure-category-on-fail args_unknown \
    --decision-reason "Invalid SCIMLOPSBENCH_MULTI_GPU_IDS='$GPU_IDS' (need >=2 comma-separated GPU ids)." \
    -- bash -lc "echo 'Invalid SCIMLOPSBENCH_MULTI_GPU_IDS=$GPU_IDS (need >=2 comma-separated GPU ids)'; exit 1"
	  exit 1
	fi

gpu_count="$(
  CUDA_RESULTS_PATH="$CUDA_RESULTS" "$RUNNER_PY" - <<'PY' 2>/dev/null || true
import json, os, pathlib
p = pathlib.Path(os.environ.get("CUDA_RESULTS_PATH",""))
if not p.exists():
    print("")
else:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        observed = data.get("observed", {}) if isinstance(data, dict) else {}
        print(observed.get("gpu_count", ""))
    except Exception:
        print("")
PY
)"
if [[ -z "${gpu_count:-}" ]]; then
  gpu_count="$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ' || true)"
fi
gpu_count="${gpu_count:-0}"
if [[ "$gpu_count" -lt "$gpu_ids_count" ]]; then
  "$RUNNER_PY" benchmark_scripts/runner.py --stage multi_gpu --task train --out-dir "$OUT_DIR" --timeout-sec 1200 --framework pytorch \
    --no-requires-python \
    --command-display "multi-gpu check (need >=${gpu_ids_count} GPUs; found ${gpu_count})" \
    --failure-category-on-fail unknown \
    --decision-reason "Need >=${gpu_ids_count} GPUs for multi-GPU run; found ${gpu_count} (from ${CUDA_RESULTS} if present; else nvidia-smi)." \
    -- bash -lc "echo 'Need >=${gpu_ids_count} GPUs for multi-GPU run; found ${gpu_count}'; exit 1"
  exit 1
fi

read_prepare_asset_path() {
  local asset_key="$1"
  local fallback="$2"
  PREP_RESULTS_PATH="$PREP_RESULTS" ASSET_KEY="$asset_key" FALLBACK="$fallback" "$RUNNER_PY" - <<'PY' 2>/dev/null || echo "$fallback"
import json
import os
import pathlib

prep = pathlib.Path(os.environ.get("PREP_RESULTS_PATH", ""))
asset = os.environ.get("ASSET_KEY", "")
fallback = os.environ.get("FALLBACK", "")

if not prep.exists():
    print(fallback)
else:
    try:
        data = json.loads(prep.read_text(encoding="utf-8"))
        assets = data.get("assets", {}) if isinstance(data, dict) else {}
        asset_obj = assets.get(asset, {}) if isinstance(assets, dict) else {}
        path = asset_obj.get("path", "") if isinstance(asset_obj, dict) else ""
        print(path or fallback)
    except Exception:
        print(fallback)
PY
}

train_dataset_path="$(read_prepare_asset_path dataset "${REPO_ROOT}/benchmark_assets/dataset/UniMER-Train")"
eval_dataset_path="${REPO_ROOT}/benchmark_assets/dataset/UniMER-Eval"
if [[ ! -d "$eval_dataset_path" ]]; then
  eval_dataset_path="$train_dataset_path"
fi
model_path="$(read_prepare_asset_path model "${REPO_ROOT}/benchmark_assets/model/FormulaNet")"

# TextProcessor uses data.text_processor.tokenizer_path, but the repo converts cfg.data with
# OmegaConf.to_container(resolve=False), so interpolations like ${model.tokenizer_path} won't resolve.
# Override to a concrete local tokenizer directory.
TOKENIZER_PATH="${REPO_ROOT}/data/unimernet_tokenizer"
if [[ ! -d "$TOKENIZER_PATH" ]]; then
  if [[ -d "${REPO_ROOT}/data/unimernet_tokenizer_distill" ]]; then
    TOKENIZER_PATH="${REPO_ROOT}/data/unimernet_tokenizer_distill"
  elif [[ -d "${REPO_ROOT}/data/tokenizer" ]]; then
    TOKENIZER_PATH="${REPO_ROOT}/data/tokenizer"
  fi
fi

pretrained_pt="$(find "$model_path" -maxdepth 3 -type f -name '*.pt' 2>/dev/null | head -n 1 || true)"
if [[ -n "$pretrained_pt" ]]; then
  model_pretrained_override="model.pretrained=${pretrained_pt}"
  decision_model="use downloaded .pt weights: ${pretrained_pt}"
else
  model_pretrained_override="model.pretrained="
  decision_model="no .pt weights found under model asset; override model.pretrained to empty for minimal run"
fi

HF_CACHE_DIR="${REPO_ROOT}/benchmark_assets/cache/hf"

"$RUNNER_PY" benchmark_scripts/runner.py \
  --stage multi_gpu \
  --task train \
  --out-dir "$OUT_DIR" \
  --timeout-sec 1200 \
  --framework pytorch \
  --prepare-results "$PREP_RESULTS" \
  --decision-reason "Entrypoint: python src/train.py (README). Multi-GPU via Lightning DDP (trainer.strategy=ddp) + 1 step; disable logger/checkpoints; dataset from prepare. Override data.text_processor.tokenizer_path=${TOKENIZER_PATH} to avoid unresolved interpolation in cfg.data. ${decision_model}" \
  --env "CUDA_VISIBLE_DEVICES=${GPU_IDS}" \
  --env "TOKENIZERS_PARALLELISM=false" \
  --env "HF_HOME=${HF_CACHE_DIR}" \
  --env "HF_DATASETS_CACHE=${HF_CACHE_DIR}/datasets" \
  --env "HF_HUB_CACHE=${HF_CACHE_DIR}/hub" \
  --env "TRANSFORMERS_CACHE=${HF_CACHE_DIR}/transformers" \
  --env "XDG_CACHE_HOME=${REPO_ROOT}/benchmark_assets/cache/xdg" \
  -- \
  "{{PYTHON}}" src/train.py \
    hydra.run.dir="${REPO_ROOT}/${OUT_DIR}/hydra" \
    hydra.job.chdir=false \
    trainer.accelerator=gpu \
    trainer.devices=${gpu_ids_count} \
    trainer.num_nodes=1 \
    +trainer.strategy=ddp \
    trainer.precision=32-true \
    +trainer.max_steps=1 \
    trainer.max_epochs=1 \
    +trainer.limit_train_batches=1 \
    trainer.val_check_interval=1 \
    +trainer.limit_val_batches=0.0 \
    +trainer.num_sanity_val_steps=0 \
    trainer.log_every_n_steps=1 \
    +trainer.enable_checkpointing=false \
    trainer.logger=false \
    trainer.callbacks=[] \
    training.lr_scheduler.num_training_steps=1 \
    training.lr_scheduler.num_warmup_steps=0 \
    data.train_batch_size=1 \
    data.val_batch_size=1 \
    data.num_workers=1 \
    data.train_dataset_path="${train_dataset_path}" \
    data.eval_dataset_path="${eval_dataset_path}" \
    data.text_processor.tokenizer_path="${TOKENIZER_PATH}" \
    ${model_pretrained_override}
