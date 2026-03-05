#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step single-GPU training run using the repository's native entrypoint.

Outputs (always written):
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

Options:
  --python <path>        Override python executable (otherwise resolved from report.json)
  --report-path <path>   Override report.json path
  --timeout-sec <sec>    Default: 600
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

out_dir="${repo_root}/build_output/single_gpu"
log_path="${out_dir}/log.txt"
mkdir -p "$out_dir"
: >"$log_path"

python_bin=""
report_path=""
timeout_sec="600"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

resolve_python() {
  if [[ -n "$python_bin" ]]; then
    echo "$python_bin"
    return 0
  fi
  python3 "${repo_root}/benchmark_scripts/runner.py" resolve-python ${report_path:+--report-path "$report_path"}
}

python_bin="$(resolve_python || true)"
if [[ -z "$python_bin" ]]; then
  python3 "${repo_root}/benchmark_scripts/runner.py" write \
    --stage single_gpu --task train --framework pytorch --timeout-sec "$timeout_sec" \
    --status failure --skip-reason not_applicable --failure-category missing_report \
    --message "single_gpu stage: could not resolve python (missing/invalid report.json). Pass --python or provide /opt/scimlopsbench/report.json." \
    --require-report
  exit 1
fi

prepare_results="${repo_root}/build_output/prepare/results.json"
if [[ ! -f "$prepare_results" ]]; then
  python3 "${repo_root}/benchmark_scripts/runner.py" write \
    --stage single_gpu --task train --framework pytorch --timeout-sec "$timeout_sec" \
    --status failure --skip-reason not_applicable --failure-category data \
    --message "single_gpu stage: missing prepare stage results at ${prepare_results}; run prepare_assets.sh first." \
    --python "$python_bin" --require-report
  exit 1
fi

readarray -t prep_fields < <(PREPARE_RESULTS="$prepare_results" python3 - <<'PY'
import json
import os
import pathlib
p = pathlib.Path(os.environ["PREPARE_RESULTS"])
data = json.loads(p.read_text(encoding="utf-8"))
print((data.get("status") or "").strip())
assets = data.get("assets") or {}
print(((assets.get("dataset") or {}).get("path") or "").strip())
print(((assets.get("model") or {}).get("path") or "").strip())
PY
)
prep_status="${prep_fields[0]:-}"
dataset_path="${prep_fields[1]:-}"
model_path="${prep_fields[2]:-}"

if [[ "$prep_status" != "success" ]]; then
  python3 "${repo_root}/benchmark_scripts/runner.py" write \
    --stage single_gpu --task train --framework pytorch --timeout-sec "$timeout_sec" \
    --status failure --skip-reason not_applicable --failure-category data \
    --message "single_gpu stage: prepare stage status=${prep_status}; cannot run training without prepared assets." \
    --python "$python_bin" --assets-from-prepare "$prepare_results" --require-report
  exit 1
fi

if [[ -z "$dataset_path" || ! -f "$dataset_path" ]]; then
  python3 "${repo_root}/benchmark_scripts/runner.py" write \
    --stage single_gpu --task train --framework pytorch --timeout-sec "$timeout_sec" \
    --status failure --skip-reason not_applicable --failure-category data \
    --message "single_gpu stage: dataset file-list missing or not found: ${dataset_path}" \
    --python "$python_bin" --assets-from-prepare "$prepare_results" --require-report
  exit 1
fi

if [[ -z "$model_path" || ! -d "$model_path" ]]; then
  python3 "${repo_root}/benchmark_scripts/runner.py" write \
    --stage single_gpu --task train --framework pytorch --timeout-sec "$timeout_sec" \
    --status failure --skip-reason not_applicable --failure-category model \
    --message "single_gpu stage: model directory missing or not found: ${model_path}" \
    --python "$python_bin" --assets-from-prepare "$prepare_results" --require-report
  exit 1
fi

# Hardware precheck (torch CUDA device count with CUDA_VISIBLE_DEVICES=0).
set +e
CUDA_VISIBLE_DEVICES=0 "$python_bin" - <<'PY' >>"$log_path" 2>&1
import sys
try:
    import torch
except Exception as e:
    print(f"torch_import_failed: {e}", file=sys.stderr)
    sys.exit(3)
if not torch.cuda.is_available():
    print("cuda_not_available", file=sys.stderr)
    sys.exit(2)
if torch.cuda.device_count() < 1:
    print("gpu_count_lt_1", file=sys.stderr)
    sys.exit(1)
print(f"gpu_count={torch.cuda.device_count()}")
PY
cuda_check_rc=$?
set -e

if [[ $cuda_check_rc -ne 0 ]]; then
  python3 "${repo_root}/benchmark_scripts/runner.py" write \
    --stage single_gpu --task train --framework pytorch --timeout-sec "$timeout_sec" \
    --status failure --skip-reason insufficient_hardware --failure-category runtime \
    --message "single_gpu stage: CUDA/GPU not available (torch check rc=${cuda_check_rc})." \
    --python "$python_bin" --assets-from-prepare "$prepare_results" --require-report
  exit 1
fi

cache_dir="${repo_root}/benchmark_assets/cache"
hf_home="${cache_dir}/hf_home"
mkdir -p "${hf_home}" "${cache_dir}/xdg" "${cache_dir}/torch"

train_cmd=(
  "$python_bin" -m torch.distributed.run
  --standalone --nproc_per_node=1
  ml_mdm/clis/train_parallel.py
  --file-list="$dataset_path"
  --multinode=0
  --output-dir="${out_dir}/outputs"
  --text-model="$model_path"
  --batch-size=1
  --num-training-steps=1
  --num_diffusion_steps=1
  --model_output_scale=0
  --config_path="configs/models/cc12m_64x64.yaml"
  --fp16=0
)

python3 "${repo_root}/benchmark_scripts/runner.py" run \
  --stage single_gpu --task train --framework pytorch --timeout-sec "$timeout_sec" \
  --python "$python_bin" --assets-from-prepare "$prepare_results" --require-report \
  --decision-reason "Repo README/tests document a minimal 1-step training run; forcing single-GPU by setting CUDA_VISIBLE_DEVICES=0." \
  --env CUDA_VISIBLE_DEVICES=0 \
  --env HF_HOME="${hf_home}" \
  --env HF_HUB_CACHE="${hf_home}/hub" \
  --env HUGGINGFACE_HUB_CACHE="${hf_home}/hub" \
  --env TRANSFORMERS_CACHE="${hf_home}/hub" \
  --env TRANSFORMERS_OFFLINE=1 \
  --env HF_DATASETS_OFFLINE=1 \
  --env XDG_CACHE_HOME="${cache_dir}/xdg" \
  --env TORCH_HOME="${cache_dir}/torch" \
  -- bash -lc "cd ml-mdm-matryoshka && ${train_cmd[*]}"
