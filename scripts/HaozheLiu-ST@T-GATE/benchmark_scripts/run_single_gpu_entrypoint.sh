#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run TGATE repo entrypoint (main.py) for exactly 1 inference step on a single GPU.

Outputs:
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

Options:
  --report-path <path>   Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --python <path>        Explicit python executable (overrides report resolution)
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="$repo_root/build_output/single_gpu"
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_override=""
log_txt="$out_dir/log.txt"
results_json="$out_dir/results.json"
git_commit="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_override="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

mkdir -p "$out_dir"

bootstrap_py="$(command -v python3 || command -v python || true)"
if [[ -z "$bootstrap_py" ]]; then
  echo "[single_gpu] python3/python not found in PATH" | tee "$log_txt" >&2
  cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "single_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "$git_commit",
    "env_vars": {"CUDA_VISIBLE_DEVICES": "0"},
    "decision_reason": "python3/python not found in PATH; cannot run runner.py"
  },
  "failure_category": "deps",
  "error_excerpt": "python3/python not found in PATH"
}
JSON
  exit 1
fi

prepare_results="$repo_root/build_output/prepare/results.json"
if [[ ! -f "$prepare_results" ]]; then
  "$bootstrap_py" "$repo_root/benchmark_scripts/runner.py" \
    --stage single_gpu --task infer --framework pytorch \
    --out-dir "$out_dir" \
    --report-path "$report_path" \
    ${python_override:+--python "$python_override"} \
    --failure-category data \
    --command "python main.py ..." \
    --decision-reason "prepare stage results.json missing; cannot locate model/prompt assets." \
    --skip-reason unknown
  exit 1
fi

selected_model="$("$bootstrap_py" - <<PY 2>/dev/null || true
import json
from pathlib import Path
d=json.loads(Path("$prepare_results").read_text(encoding="utf-8"))
print(d.get("meta", {}).get("selected", {}).get("main_model_arg", ""))
PY
)"
input_type="$("$bootstrap_py" - <<PY 2>/dev/null || true
import json
from pathlib import Path
d=json.loads(Path("$prepare_results").read_text(encoding="utf-8"))
print(d.get("meta", {}).get("selected", {}).get("input_type", "prompt"))
PY
)"

dataset_path="$("$bootstrap_py" - <<PY 2>/dev/null || true
import json
from pathlib import Path
d=json.loads(Path("$prepare_results").read_text(encoding="utf-8"))
print(d.get("assets", {}).get("dataset", {}).get("path", ""))
PY
)"

prompt=""
image_path=""
if [[ "$input_type" == "image" ]]; then
  image_path="$("$bootstrap_py" - <<PY 2>/dev/null || true
import json
from pathlib import Path
d=json.loads(Path("$prepare_results").read_text(encoding="utf-8"))
print(d.get("meta", {}).get("selected", {}).get("image_path", ""))
PY
)"
else
  if [[ -n "$dataset_path" && -f "$dataset_path" ]]; then
    prompt="$(head -n 1 "$dataset_path" 2>/dev/null || true)"
  fi
fi

saved_path="$out_dir/generated"

env_args=(
  --env "CUDA_VISIBLE_DEVICES=0"
  --env "HF_HOME=$repo_root/benchmark_assets/cache/hf_home"
  --env "HUGGINGFACE_HUB_CACHE=$repo_root/benchmark_assets/cache/hf_home/hub"
  --env "TRANSFORMERS_CACHE=$repo_root/benchmark_assets/cache/hf_home/transformers"
  --env "DIFFUSERS_CACHE=$repo_root/benchmark_assets/cache/hf_home/diffusers"
  --env "HF_DATASETS_CACHE=$repo_root/benchmark_assets/cache/hf_home/datasets"
  --env "XDG_CACHE_HOME=$repo_root/benchmark_assets/cache/xdg"
  --env "TORCH_HOME=$repo_root/benchmark_assets/cache/torch"
  --env "PIP_CACHE_DIR=$repo_root/benchmark_assets/cache/pip"
  --env "HF_HUB_DISABLE_TELEMETRY=1"
  --env "TOKENIZERS_PARALLELISM=false"
)

decision_reason="Use TGATE repo entrypoint main.py for minimal single-GPU inference (1 prompt/image, inference_step=1); force CUDA_VISIBLE_DEVICES=0."

if [[ "$input_type" == "image" ]]; then
  "$bootstrap_py" "$repo_root/benchmark_scripts/runner.py" \
    --stage single_gpu --task infer --framework pytorch \
    --out-dir "$out_dir" \
    --report-path "$report_path" \
    ${python_override:+--python "$python_override"} \
    --requires-python \
    --assets-from "$prepare_results" \
    --decision-reason "$decision_reason" \
    "${env_args[@]}" \
    --python-script "main.py" -- \
      --model "$selected_model" \
      --image "$image_path" \
      --saved_path "$saved_path" \
      --inference_step 1 \
      --gate_step 1 \
      --sp_interval 1 \
      --fi_interval 1 \
      --warm_up 0
else
  "$bootstrap_py" "$repo_root/benchmark_scripts/runner.py" \
    --stage single_gpu --task infer --framework pytorch \
    --out-dir "$out_dir" \
    --report-path "$report_path" \
    ${python_override:+--python "$python_override"} \
    --requires-python \
    --assets-from "$prepare_results" \
    --decision-reason "$decision_reason" \
    "${env_args[@]}" \
    --python-script "main.py" -- \
      --model "$selected_model" \
      --prompt "$prompt" \
      --saved_path "$saved_path" \
      --inference_step 1 \
      --gate_step 1 \
      --sp_interval 1 \
      --fi_interval 1 \
      --warm_up 0
fi
