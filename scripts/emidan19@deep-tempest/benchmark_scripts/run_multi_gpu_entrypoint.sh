#!/usr/bin/env bash
set -euo pipefail

stage="multi_gpu"
task="train"
timeout_sec="${SCIMLOPSBENCH_MULTI_GPU_TIMEOUT_SEC:-1200}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sys_python="$(command -v python3 || command -v python || true)"

if [[ -z "${sys_python}" ]]; then
  echo "ERROR: python3/python not found in PATH" >&2
  exit 1
fi

cd "${repo_root}"

visible_devices_default="${SCIMLOPSBENCH_MULTI_GPU_VISIBLE_DEVICES:-0,1}"
nproc_default="${SCIMLOPSBENCH_MULTI_GPU_NPROC_PER_NODE:-2}"
cli_python=""
visible_devices="${visible_devices_default}"
nproc="${nproc_default}"

usage() {
  cat <<'EOF'
run_multi_gpu_entrypoint.sh

Optional:
  --python <path>          Override python used for the run (else uses SCIMLOPSBENCH_PYTHON or report.json python_path)
  --gpus <csv>             CUDA_VISIBLE_DEVICES list (default: 0,1 or $SCIMLOPSBENCH_MULTI_GPU_VISIBLE_DEVICES)
  --nproc <int>            Processes per node (default: 2 or $SCIMLOPSBENCH_MULTI_GPU_NPROC_PER_NODE)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      cli_python="${2:-}"; shift 2 ;;
    --gpus)
      visible_devices="${2:-}"; shift 2 ;;
    --nproc)
      nproc="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

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
    --decision-reason "Multi-GPU run uses assets prepared by benchmark_scripts/prepare_assets.sh." \
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
model_file = meta.get("resolved_model_file") or ""
print(dataset_root)
print(model_file)
PY
)

mapfile -t lines < <("${sys_python}" -c "${read_assets_py}" "${prepare_results}")
dataset_root="${lines[0]:-}"
model_file="${lines[1]:-}"

if [[ -z "${dataset_root}" ]]; then
  "${sys_python}" "${repo_root}/benchmark_scripts/runner.py" fail \
    --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
    --failure-category "data" \
    --decision-reason "prepare stage must report assets.dataset.path." \
    --error-message "Invalid prepare results: missing assets.dataset.path." \
    --command "${prepare_results}" \
    --assets-from "${prepare_results}" \
    >/dev/null 2>&1 || true
  exit 1
fi

mini_h="${dataset_root}/mini_2/H"
mini_l="${dataset_root}/mini_2/L"
if [[ ! -d "${mini_h}" || ! -d "${mini_l}" ]]; then
  "${sys_python}" "${repo_root}/benchmark_scripts/runner.py" fail \
    --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
    --failure-category "data" \
    --decision-reason "Multi-GPU one-step uses benchmark_assets/dataset/mini_2 with 2 paired samples." \
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
    --decision-reason "Multi-GPU one-step loads pretrained weights downloaded in prepare stage." \
    --error-message "Resolved model file missing: ${model_file} (prepare meta.resolved_model_file)" \
    --command "prepare_assets.sh" \
    --assets-from "${prepare_results}" \
    >/dev/null 2>&1 || true
  exit 1
fi

# Resolve python (same logic as runner.py) so we can pre-check GPU count.
resolve_py=$(
  cat <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path("benchmark_scripts").resolve()))
import runner

cli_python = os.environ.get("CLI_PYTHON") or None
report_path = runner.resolve_report_path(None)
res, err = runner.resolve_python(cli_python=cli_python, report_path=report_path)
if res is None:
    print(f"ERROR:{err}", file=sys.stderr)
    raise SystemExit(1)
print(res.python)
PY
)

resolved_python="$(CLI_PYTHON="${cli_python}" "${sys_python}" -c "${resolve_py}")"

if [[ -z "${resolved_python}" ]]; then
  "${sys_python}" "${repo_root}/benchmark_scripts/runner.py" fail \
    --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
    --failure-category "missing_report" \
    --decision-reason "runner-style python resolution is required for multi-GPU stage." \
    --error-message "Unable to resolve python (need report.json python_path, SCIMLOPSBENCH_PYTHON, or --python)." \
    --command "resolve_python" \
    --assets-from "${prepare_results}" \
    >/dev/null 2>&1 || true
  exit 1
fi

echo "[multi_gpu] Using python: ${resolved_python}"
echo "[multi_gpu] CUDA_VISIBLE_DEVICES=${visible_devices}"

set +e
gpu_count_out="$(CUDA_VISIBLE_DEVICES="${visible_devices}" PYTHONWARNINGS=ignore "${resolved_python}" -c 'import warnings; warnings.filterwarnings("ignore"); import torch; print(torch.cuda.device_count())' 2>&1)"
gpu_probe_rc=$?
set -e

if [[ "${gpu_probe_rc}" -ne 0 ]]; then
  "${sys_python}" "${repo_root}/benchmark_scripts/runner.py" fail \
    --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
    --failure-category "deps" \
    --decision-reason "GPU count detection uses torch in the resolved benchmark python." \
    --error-message "torch import / cuda probe failed (rc=${gpu_probe_rc}). ${gpu_count_out}" \
    --command "${resolved_python} -c 'import torch; print(torch.cuda.device_count())'" \
    --assets-from "${prepare_results}" \
    >/dev/null 2>&1 || true
  exit 1
fi

gpu_count="$(printf '%s' "${gpu_count_out}" | tr -d '[:space:]' || true)"
if [[ -z "${gpu_count}" ]]; then
  gpu_count="0"
fi

if [[ "${gpu_count}" -lt 2 ]]; then
  "${sys_python}" "${repo_root}/benchmark_scripts/runner.py" fail \
    --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
    --failure-category "insufficient_hardware" \
    --decision-reason "Multi-GPU stage requires >=2 visible GPUs (CUDA_VISIBLE_DEVICES=${visible_devices})." \
    --error-message "Need >=2 GPUs; observed gpu_count=${gpu_count}." \
    --command "${resolved_python} -c 'import torch; print(torch.cuda.device_count())'" \
    --assets-from "${prepare_results}" \
    >/dev/null 2>&1 || true
  exit 1
fi

out_dir="${repo_root}/build_output/${stage}"
mkdir -p "${out_dir}"
opt_path="${out_dir}/options_multi_gpu_min.json"

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
  "gpu_ids": [0, 1],
  "scale": 0,
  "n_channels": 1,
  "n_channels_datasetload": 3,
  "use_abs_value": True,
  "path": {
    "root": "build_output/multi_gpu/artifacts",
    "pretrained_netG": model_file
  },
  "datasets": {
    "train": {
      "name": "mini_train_multi_gpu",
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
      "dataloader_batch_size": 2
    },
    "test": {
      "name": "mini_test_multi_gpu",
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
      "dataloader_batch_size": 2
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

set +e
${sys_python} "${repo_root}/benchmark_scripts/runner.py" run \
  --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
  --assets-from "${prepare_results}" \
  --decision-reason "Attempt DDP via torch.distributed.run with --dist True. Use a 2-sample dataset and global batch_size=2 so each of 2 GPUs processes batch_size=1 for exactly one training iteration." \
  --env "PYTHONPATH=end-to-end" \
  --env "CUDA_VISIBLE_DEVICES=${visible_devices}" \
  --env "OMP_NUM_THREADS=1" \
  --env "MKL_NUM_THREADS=1" \
  -- \
  "{python}" "-m" "torch.distributed.run" "--standalone" "--nnodes" "1" "--nproc_per_node" "${nproc}" \
    "end-to-end/main_train_drunet.py" "--opt" "${opt_path}" "--dist" "True"
ddp_rc=$?
set -e
if [[ "${ddp_rc}" -eq 0 ]]; then
  exit 0
fi

# If the repo's dist=True code path fails due to the known sampler/shuffle conflict,
# fall back to multi-GPU DataParallel (dist=False) in a single process.
ddp_log="${out_dir}/ddp_attempt.log"
ddp_results="${out_dir}/ddp_attempt.results.json"

if grep -q "sampler option is mutually exclusive with shuffle" "${out_dir}/log.txt" 2>/dev/null; then
  cp -f "${out_dir}/log.txt" "${ddp_log}" || true
  cp -f "${out_dir}/results.json" "${ddp_results}" || true

  echo "[multi_gpu] Detected DataLoader sampler/shuffle conflict in dist=True path; retrying with DataParallel (dist=False)."
  ${sys_python} "${repo_root}/benchmark_scripts/runner.py" run \
    --stage "${stage}" --task "${task}" --framework "pytorch" --timeout-sec "${timeout_sec}" \
    --assets-from "${prepare_results}" \
    --decision-reason "Repo dist=True path failed with 'sampler option is mutually exclusive with shuffle'; run multi-GPU via DataParallel instead (dist=False, gpu_ids=[0,1], CUDA_VISIBLE_DEVICES=${visible_devices}), still one iteration using 2-sample dataset and batch_size=2." \
    --env "PYTHONPATH=end-to-end" \
    --env "CUDA_VISIBLE_DEVICES=${visible_devices}" \
    --env "OMP_NUM_THREADS=1" \
    --env "MKL_NUM_THREADS=1" \
    -- \
    "{python}" "end-to-end/main_train_drunet.py" "--opt" "${opt_path}"
  exit $?
fi

exit "${ddp_rc}"
