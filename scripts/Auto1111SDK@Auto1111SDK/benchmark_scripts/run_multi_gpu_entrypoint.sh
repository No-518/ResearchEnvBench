#!/usr/bin/env bash
set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

usage() {
  cat <<'EOF'
Run a minimal 1-step multi-GPU (2 GPUs) training run via the repository's native entrypoint.

Forces (default):
  CUDA_VISIBLE_DEVICES=0,1

Entrypoint:
  auto1111sdk/modules/generative/main.py

Distributed launch:
  PyTorch Lightning (DDPStrategy configured by the entrypoint)

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Options:
  --cuda-visible-devices <list>  Override visible GPU list (default: 0,1)
  --python <path>                Override python (bypasses report.json)
  --report-path <path>           Override report.json path (default: /opt/scimlopsbench/report.json)
  --timeout-sec <sec>            Default: 1200
EOF
}

cuda_visible_devices="0,1"
python_override=""
report_path=""
timeout_sec="1200"
py_runner="python"

if ! command -v "$py_runner" >/dev/null 2>&1; then
  py_runner="python3"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cuda-visible-devices)
      cuda_visible_devices="${2:-}"; shift 2 ;;
    --python)
      python_override="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --timeout-sec)
      timeout_sec="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="$repo_root/build_output/multi_gpu"
extra_json="$out_dir/extra.json"

mkdir -p "$out_dir"

runner_args=(
  --stage multi_gpu
  --task train
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --out-dir "$out_dir"
  --extra-json "$extra_json"
  --decision-reason "Run auto1111sdk/modules/generative/main.py with MNIST toy config; multi-GPU via Lightning DDPStrategy; forced CUDA_VISIBLE_DEVICES=${cuda_visible_devices}; batch_size=1, max_steps=1."
  --failure-category runtime
)
if [[ -n "$python_override" ]]; then
  runner_args+=(--python "$python_override")
fi
if [[ -n "$report_path" ]]; then
  runner_args+=(--report-path "$report_path")
fi

cmd_str="$(cat <<BASH
set -euo pipefail
export CUDA_VISIBLE_DEVICES="${cuda_visible_devices}"

OUT_DIR="\${SCIMLOPSBENCH_REPO_ROOT}/build_output/multi_gpu"
EXTRA_JSON="\${OUT_DIR}/extra.json"
PREP_RESULTS="\${SCIMLOPSBENCH_REPO_ROOT}/build_output/prepare/results.json"
export OUT_DIR EXTRA_JSON PREP_RESULTS

eval "\$("\${SCIMLOPSBENCH_PYTHON_RESOLVED}" -B - <<'PY'
import json
import os
import pathlib
import shlex
import sys

repo_root = pathlib.Path(os.environ.get("SCIMLOPSBENCH_REPO_ROOT", ".")).resolve()
prep_results = pathlib.Path(os.environ["PREP_RESULTS"])
extra_path = pathlib.Path(os.environ["EXTRA_JSON"])

assets = {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
}
meta = {
    "prepare_results_path": str(prep_results),
    "dataset_dir": "",
    "model_cfg": "",
    "visible_gpus": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
}

prepare_read_error = None
try:
    data = json.loads(prep_results.read_text(encoding="utf-8"))
    a = data.get("assets", {})
    if isinstance(a, dict):
        for k in ("dataset", "model"):
            v = a.get(k)
            if isinstance(v, dict):
                assets[k].update(v)
except Exception as e:
    prepare_read_error = f"{type(e).__name__}: {e}"

dataset_dir = str(assets["dataset"].get("path") or "")
model_path = str(assets["model"].get("path") or "")

model_cfg = ""
mp = pathlib.Path(model_path) if model_path else None
if mp and mp.exists():
    if mp.is_dir():
        candidate = mp / "mnist.yaml"
        if candidate.exists():
            model_cfg = str(candidate)
    elif mp.is_file():
        model_cfg = str(mp)

meta["dataset_dir"] = dataset_dir
meta["model_cfg"] = model_cfg

extra = {"assets": assets, "meta": {"runtime": meta}}
if prepare_read_error:
    extra["meta"]["prepare_read_error"] = prepare_read_error

paths_ok = bool(dataset_dir and pathlib.Path(dataset_dir).is_dir() and model_cfg and pathlib.Path(model_cfg).is_file())
if not paths_ok:
    extra["failure_category"] = "data"
    extra["meta"]["prepare_error"] = "missing dataset_dir/model_cfg; run prepare_assets.sh first"

gpu_count = 0
torch_err = None
try:
    import torch
    gpu_count = int(torch.cuda.device_count())
except Exception as e:
    torch_err = f"{type(e).__name__}: {e}"
meta["torch_visible_gpu_count"] = gpu_count
if torch_err:
    meta["torch_error"] = torch_err

gpus_ok = gpu_count >= 2
if paths_ok and not gpus_ok:
    extra["failure_category"] = "runtime"
    extra["meta"]["insufficient_hardware"] = f"need >=2 visible GPUs; found {gpu_count}"

extra_path.parent.mkdir(parents=True, exist_ok=True)
extra_path.write_text(json.dumps(extra, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")

if not paths_ok:
    sys.exit(1)
if not gpus_ok:
    sys.exit(1)

print(f"DATASET_DIR={shlex.quote(dataset_dir)}")
print(f"MODEL_CFG={shlex.quote(model_cfg)}")
PY
)"

echo "[multi_gpu] dataset_dir=\${DATASET_DIR}"
echo "[multi_gpu] model_cfg=\${MODEL_CFG}"
echo "[multi_gpu] CUDA_VISIBLE_DEVICES=\${CUDA_VISIBLE_DEVICES}"

cd "\${DATASET_DIR}"

"\${SCIMLOPSBENCH_PYTHON_RESOLVED}" "\${SCIMLOPSBENCH_REPO_ROOT}/auto1111sdk/modules/generative/main.py" \
  --base "\${MODEL_CFG}" \
  --no_base_name True \
  --train True \
  --no-test True \
  --logdir "\${OUT_DIR}/lightning_logs" \
  --accelerator gpu \
  --devices "0,1" \
  data.params.batch_size=1 \
  data.params.num_workers=0 \
  lightning.trainer.max_steps=1 \
  lightning.trainer.limit_train_batches=1 \
  lightning.trainer.limit_val_batches=0 \
  lightning.trainer.num_sanity_val_steps=0 \
  lightning.callbacks.image_logger.params.disabled=true
BASH
)"

"$py_runner" "${repo_root}/benchmark_scripts/runner.py" "${runner_args[@]}" -- bash -lc "$cmd_str"
