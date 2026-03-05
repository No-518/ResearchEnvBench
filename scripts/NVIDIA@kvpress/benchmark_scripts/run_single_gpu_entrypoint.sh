#!/usr/bin/env bash
set -u -o pipefail

usage() {
  cat <<'EOF'
Run the repository's native entrypoint for a minimal 1-sample single-GPU inference run.

Entrypoint used (kvpress): evaluation/evaluate.py

Outputs:
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json
EOF
}

python_bin=""
report_path=""
timeout_sec="600"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
cd "$repo_root"

manifest="benchmark_assets/manifest.json"
if [[ ! -f "$manifest" ]]; then
  mkdir -p build_output/single_gpu
  echo "prepare_assets.sh must be run first (missing benchmark_assets/manifest.json)" >build_output/single_gpu/log.txt
  python3 - <<'PY' >build_output/single_gpu/results.json
import json
print(json.dumps({
  "status":"failure","skip_reason":"not_applicable","exit_code":1,
  "stage":"single_gpu","task":"infer","command":"",
  "timeout_sec":600,"framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"Missing benchmark_assets/manifest.json"},
  "failure_category":"data","error_excerpt":"Missing benchmark_assets/manifest.json"
}, indent=2))
PY
  exit 1
fi

dataset_name="$(python3 -c "import json;print(json.load(open('$manifest'))['dataset'].get('name',''))" 2>/dev/null || true)"
data_dir="$(python3 -c "import json;print(json.load(open('$manifest'))['dataset'].get('data_dir','') or '')" 2>/dev/null || true)"
model_path="$(python3 -c "import json;print(json.load(open('$manifest'))['model'].get('path',''))" 2>/dev/null || true)"
press_name="$(python3 -c "import json;print(json.load(open('$manifest'))['evaluation'].get('press_name','knorm'))" 2>/dev/null || true)"
compression_ratio="$(python3 -c "import json;print(json.load(open('$manifest'))['evaluation'].get('compression_ratio',0.5))" 2>/dev/null || true)"
fraction="$(python3 -c "import json;print(json.load(open('$manifest'))['evaluation'].get('fraction',1.0))" 2>/dev/null || true)"
max_new_tokens="$(python3 -c "import json;print(json.load(open('$manifest'))['evaluation'].get('max_new_tokens',1))" 2>/dev/null || true)"
max_context_length="$(python3 -c "import json;print(json.load(open('$manifest'))['evaluation'].get('max_context_length',128))" 2>/dev/null || true)"
seed="$(python3 -c "import json;print(json.load(open('$manifest'))['evaluation'].get('seed',42))" 2>/dev/null || true)"

export PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}"
export HF_HOME="$repo_root/benchmark_assets/cache/hf_home"
export HF_HUB_CACHE="$repo_root/benchmark_assets/cache/hf_hub"
export HUGGINGFACE_HUB_CACHE="$repo_root/benchmark_assets/cache/hf_hub"
export TRANSFORMERS_CACHE="$repo_root/benchmark_assets/cache/transformers"
export HF_DATASETS_CACHE="$repo_root/benchmark_assets/cache/datasets"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export TOKENIZERS_PARALLELISM=false
export CUDA_VISIBLE_DEVICES="0"

out_dir="build_output/single_gpu/eval_output"
mkdir -p "$out_dir"

cmd=(evaluation/evaluate.py
  --dataset "$dataset_name"
  --model "$model_path"
  --press_name "$press_name"
  --compression_ratio "$compression_ratio"
  --fraction "$fraction"
  --max_new_tokens "$max_new_tokens"
  --max_context_length "$max_context_length"
  --seed "$seed"
  --device "cuda:0"
  --output_dir "$out_dir"
  --log_level "INFO"
)
if [[ -n "$data_dir" ]]; then
  cmd+=(--data_dir "$data_dir")
fi

runner=(python3 benchmark_scripts/runner.py
  --stage single_gpu
  --task infer
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --assets-manifest "$manifest"
  --decision-reason "kvpress evaluation/evaluate.py one-sample inference on single GPU via --device cuda:0; CUDA_VISIBLE_DEVICES=0."
)
if [[ -n "$python_bin" ]]; then
  runner+=(--python "$python_bin")
fi
if [[ -n "$report_path" ]]; then
  runner+=(--report-path "$report_path")
fi
runner+=(--use-resolved-python -- "${cmd[@]}")

exec "${runner[@]}"

