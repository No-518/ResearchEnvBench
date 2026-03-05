#!/usr/bin/env bash
set -euo pipefail

stage="cpu"
task="train"
timeout_sec="${SCIMLOPSBENCH_CPU_TIMEOUT_SEC:-600}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sys_python="$(command -v python3 || command -v python || true)"

if [[ -z "${sys_python}" ]]; then
  echo "ERROR: python3/python not found in PATH" >&2
  exit 1
fi

entrypoint="${repo_root}/end-to-end/main_train_drunet.py"
if [[ ! -f "${entrypoint}" ]]; then
  "${sys_python}" "${repo_root}/benchmark_scripts/runner.py" fail \
    --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
    --failure-category "entrypoint_not_found" \
    --decision-reason "Repository training entrypoint expected at end-to-end/main_train_drunet.py per end-to-end/README.md." \
    --error-message "Missing entrypoint: ${entrypoint}" \
    --command "python end-to-end/main_train_drunet.py" \
    >/dev/null 2>&1 || true
  exit 1
fi

prepare_results="${repo_root}/build_output/prepare/results.json"
if [[ ! -f "${prepare_results}" ]]; then
  "${sys_python}" "${repo_root}/benchmark_scripts/runner.py" fail \
    --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
    --failure-category "data" \
    --decision-reason "CPU run uses assets prepared by benchmark_scripts/prepare_assets.sh." \
    --error-message "Missing ${prepare_results}; run benchmark_scripts/prepare_assets.sh first." \
    --command "prepare_assets.sh" \
    >/dev/null 2>&1 || true
  exit 1
fi

read_assets_py=$(
  cat <<'PY'
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
data = json.loads(p.read_text(encoding="utf-8"))
assets = data.get("assets") or {}
meta = data.get("meta") or {}
dataset_root = (assets.get("dataset") or {}).get("path") or ""
model_root = (assets.get("model") or {}).get("path") or ""
model_file = meta.get("resolved_model_file") or ""
print(dataset_root)
print(model_root)
print(model_file)
PY
)

mapfile -t lines < <("${sys_python}" -c "${read_assets_py}" "${prepare_results}")
dataset_root="${lines[0]:-}"
model_root="${lines[1]:-}"
model_file="${lines[2]:-}"

if [[ -z "${dataset_root}" || -z "${model_root}" ]]; then
  "${sys_python}" "${repo_root}/benchmark_scripts/runner.py" fail \
    --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
    --failure-category "data" \
    --decision-reason "prepare stage must report assets.dataset.path and assets.model.path." \
    --error-message "Invalid prepare results: missing assets paths." \
    --command "${prepare_results}" \
    --assets-from "${prepare_results}" \
    >/dev/null 2>&1 || true
  exit 1
fi

mini_h="${dataset_root}/mini_1/H"
mini_l="${dataset_root}/mini_1/L"
if [[ ! -d "${mini_h}" || ! -d "${mini_l}" ]]; then
  "${sys_python}" "${repo_root}/benchmark_scripts/runner.py" fail \
    --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
    --failure-category "data" \
    --decision-reason "CPU one-step uses benchmark_assets/dataset/mini_1 with exactly one paired sample." \
    --error-message "Missing dataset dirs: ${mini_h} and/or ${mini_l}" \
    --command "prepare_assets.sh" \
    --assets-from "${prepare_results}" \
    >/dev/null 2>&1 || true
  exit 1
fi

if [[ -z "${model_file}" || ! -f "${model_file}" ]]; then
  "${sys_python}" "${repo_root}/benchmark_scripts/runner.py" fail \
    --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
    --failure-category "model" \
    --decision-reason "CPU one-step loads pretrained weights downloaded in prepare stage." \
    --error-message "Resolved model file missing: ${model_file} (prepare meta.resolved_model_file)" \
    --command "prepare_assets.sh" \
    --assets-from "${prepare_results}" \
    >/dev/null 2>&1 || true
  exit 1
fi

out_dir="${repo_root}/build_output/${stage}"
mkdir -p "${out_dir}"
opt_path="${out_dir}/options_cpu_min.json"

MODEL_FILE="${model_file}" MINI_H="${mini_h}" MINI_L="${mini_l}" OPT_PATH="${opt_path}" ${sys_python} - <<'PY'
import json
import os
from pathlib import Path

model_file = os.environ["MODEL_FILE"]
mini_h = os.environ["MINI_H"]
mini_l = os.environ["MINI_L"]
opt_path = os.environ["OPT_PATH"]

opt = {
  "task": "drunet",
  "model": "plain",
  "gpu_ids": [],
  "scale": 0,
  "n_channels": 1,
  "n_channels_datasetload": 3,
  "use_abs_value": True,
  "path": {
    "root": "build_output/cpu/artifacts",
    "pretrained_netG": model_file
  },
  "datasets": {
    "train": {
      "name": "mini_train_cpu",
      "dataset_type": "drunet",
      "dataroot_H": mini_h,
      "dataroot_L": mini_l,
      "sigma": [0, 0],
      "use_all_patches": False,
      "skip_natural_patches": False,
      "num_patches_per_image": 1,
      "H_size": 128,
      "dataloader_shuffle": False,
      "dataloader_num_workers": 0,
      "dataloader_batch_size": 1
    },
    "test": {
      "name": "mini_test_cpu",
      "dataset_type": "drunet",
      "dataroot_H": mini_h,
      "dataroot_L": mini_l,
      "sigma_test": 0,
      "use_all_patches": False,
      "skip_natural_patches": False,
      "num_patches_per_image": 1,
      "H_size": 128,
      "dataloader_shuffle": False,
      "dataloader_num_workers": 0,
      "dataloader_batch_size": 1
    }
  },
  "netG": {
    "net_type": "drunet",
    "in_nc": 2,
    "out_nc": 1,
    "nc": [64, 128, 256, 512],
    "nb": 4,
    "gc": 32,
    "ng": 2,
    "reduction": 16,
    "act_mode": "R",
    "upsample_mode": "convtranspose",
    "downsample_mode": "strideconv",
    "bias": False,
    "init_type": "kaiming_normal",
    "init_bn_type": "uniform",
    "init_gain": 0.2
  },
  "train": {
    "manual_seed": 123,
    "epochs": 1,
    "G_lossfn_type": "l2",
    "G_lossfn_weight": 1.0,
    "G_tvloss_weight": 0.0,
    "G_tvloss_reduction": "mean",
    "G_optimizer_type": "adam",
    "G_optimizer_lr": 1e-4,
    "G_optimizer_clipgrad": None,
    "G_scheduler_type": "MultiStepLR",
    "G_scheduler_milestones": [],
    "G_scheduler_gamma": 0.1,
    "checkpoint_test": 999999,
    "checkpoint_test_save": 999999,
    "checkpoint_save": 999999,
    "checkpoint_print": 999999,
    "E_decay": 0,
    "G_optimizer_betas": [0.9, 0.999],
    "G_optimizer_wd": 0,
    "G_optimizer_reuse": False,
    "G_param_strict": True
  }
}

Path(opt_path).write_text(json.dumps(opt, indent=2) + "\n", encoding="utf-8")
PY

${sys_python} "${repo_root}/benchmark_scripts/runner.py" run \
  --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
  --assets-from "${prepare_results}" \
  --decision-reason "Use end-to-end/main_train_drunet.py with a generated options JSON to force CPU (gpu_ids=[]), batch_size=1, and exactly one training iteration via a 1-sample dataset." \
  --env "PYTHONPATH=end-to-end" \
  --env "CUDA_VISIBLE_DEVICES=" \
  --env "OMP_NUM_THREADS=1" \
  --env "MKL_NUM_THREADS=1" \
  -- \
  "{python}" "end-to-end/main_train_drunet.py" "--opt" "${opt_path}"
