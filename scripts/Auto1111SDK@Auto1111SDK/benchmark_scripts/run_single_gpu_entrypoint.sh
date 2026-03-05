#!/usr/bin/env bash
set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

usage() {
  cat <<'EOF'
Run a minimal 1-step single-GPU training run via the repository's native entrypoint.

Forces:
  CUDA_VISIBLE_DEVICES=0

Entrypoint:
  auto1111sdk/modules/generative/main.py

Outputs:
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

Options:
  --python <path>        Override python (bypasses report.json)
  --report-path <path>  Override report.json path (default: /opt/scimlopsbench/report.json)
  --timeout-sec <sec>   Default: 600
EOF
}

python_override=""
report_path=""
timeout_sec="600"
py_runner="python"

if ! command -v "$py_runner" >/dev/null 2>&1; then
  py_runner="python3"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
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
out_dir="$repo_root/build_output/single_gpu"
extra_json="$out_dir/extra.json"

mkdir -p "$out_dir"

runner_args=(
  --stage single_gpu
  --task train
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --out-dir "$out_dir"
  --extra-json "$extra_json"
  --decision-reason "Run auto1111sdk/modules/generative/main.py with MNIST toy config; forced single GPU via CUDA_VISIBLE_DEVICES=0; batch_size=1, max_steps=1."
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
export CUDA_VISIBLE_DEVICES="0"

OUT_DIR="\${SCIMLOPSBENCH_REPO_ROOT}/build_output/single_gpu"
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

ok = bool(dataset_dir and pathlib.Path(dataset_dir).is_dir() and model_cfg and pathlib.Path(model_cfg).is_file())
if not ok:
    extra["failure_category"] = "data"
    extra["meta"]["prepare_error"] = "missing dataset_dir/model_cfg; run prepare_assets.sh first"

extra_path.parent.mkdir(parents=True, exist_ok=True)
extra_path.write_text(json.dumps(extra, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")

if not ok:
    sys.exit(1)

print(f"DATASET_DIR={shlex.quote(dataset_dir)}")
print(f"MODEL_CFG={shlex.quote(model_cfg)}")
PY
)"

echo "[single_gpu] dataset_dir=\${DATASET_DIR}"
echo "[single_gpu] model_cfg=\${MODEL_CFG}"

cd "\${DATASET_DIR}"

"\${SCIMLOPSBENCH_PYTHON_RESOLVED}" "\${SCIMLOPSBENCH_REPO_ROOT}/auto1111sdk/modules/generative/main.py" \
  --base "\${MODEL_CFG}" \
  --no_base_name True \
  --train True \
  --no-test True \
  --logdir "\${OUT_DIR}/lightning_logs" \
  --accelerator gpu \
  --devices "0" \
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
