#!/usr/bin/env bash
set -u -o pipefail

usage() {
  cat <<'EOF'
Run the repository's native entrypoint for a minimal 1-sample multi-GPU inference run.

Entrypoint used (kvpress): evaluation/evaluate.py
Multi-GPU mechanism (kvpress): transformers pipeline with device_map="auto" (evaluate.py uses --device auto).

Requirements enforced:
  - GPU count must be >= 2 (else exit 1)
  - CUDA_VISIBLE_DEVICES defaults to 0,1 (override via --gpus)

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json
EOF
}

python_bin=""
report_path=""
timeout_sec="1200"
gpus="0,1"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    --gpus) gpus="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
cd "$repo_root"

manifest="benchmark_assets/manifest.json"
out_dir_root="build_output/multi_gpu"
mkdir -p "$out_dir_root"

if [[ ! -f "$manifest" ]]; then
  echo "prepare_assets.sh must be run first (missing benchmark_assets/manifest.json)" >"$out_dir_root/log.txt"
  python3 - <<'PY' >"$out_dir_root/results.json"
import json
print(json.dumps({
  "status":"failure","skip_reason":"not_applicable","exit_code":1,
  "stage":"multi_gpu","task":"infer","command":"",
  "timeout_sec":1200,"framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"Missing benchmark_assets/manifest.json"},
  "failure_category":"data","error_excerpt":"Missing benchmark_assets/manifest.json"
}, indent=2))
PY
  exit 1
fi

# Resolve python to check GPU count.
python_exe=""
if [[ -n "$python_bin" ]]; then
  python_exe="$python_bin"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  python_exe="$SCIMLOPSBENCH_PYTHON"
else
  rp="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
  if [[ -f "$rp" ]]; then
    python_exe="$(python3 - <<PY 2>/dev/null
import json
try:
  d = json.load(open("$rp", "r", encoding="utf-8"))
  print(d.get("python_path","") or "")
except Exception:
  print("")
PY
)"
  fi
fi

if [[ -z "$python_exe" ]]; then
  echo "missing/invalid report for python resolution" >"$out_dir_root/log.txt"
  python3 - <<PY >"$out_dir_root/results.json"
import json
print(json.dumps({
  "status":"failure","skip_reason":"not_applicable","exit_code":1,
  "stage":"multi_gpu","task":"infer","command":"",
  "timeout_sec":1200,"framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"Missing report python_path; cannot check GPU count."},
  "failure_category":"missing_report","error_excerpt":"Missing report python_path."
}, indent=2))
PY
  exit 1
fi

gpu_count="$("$python_exe" - <<'PY' 2>/dev/null || echo 0
try:
  import torch
  print(int(torch.cuda.device_count()))
except Exception:
  print(0)
PY
)"
if [[ "$gpu_count" -lt 2 ]]; then
  echo "Need >=2 GPUs for multi-GPU stage; detected gpu_count=$gpu_count" >"$out_dir_root/log.txt"
  python3 - <<PY >"$out_dir_root/results.json"
import json, os
print(json.dumps({
  "status":"failure",
  "skip_reason":"insufficient_hardware",
  "exit_code":1,
  "stage":"multi_gpu",
  "task":"infer",
  "command":"",
  "timeout_sec":1200,
  "framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":os.environ.get("SCIMLOPSBENCH_PYTHON",""),"git_commit":"","env_vars":{},"decision_reason":"Detected <2 GPUs; cannot run multi-GPU."},
  "failure_category":"unknown",
  "error_excerpt":"Need >=2 GPUs for multi-GPU stage."
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
export CUDA_VISIBLE_DEVICES="$gpus"

eval_out="build_output/multi_gpu/eval_output"
mkdir -p "$eval_out"

cmd=(evaluation/evaluate.py
  --dataset "$dataset_name"
  --model "$model_path"
  --press_name "$press_name"
  --compression_ratio "$compression_ratio"
  --fraction "$fraction"
  --max_new_tokens "$max_new_tokens"
  --max_context_length "$max_context_length"
  --seed "$seed"
  --device "auto"
  --output_dir "$eval_out"
  --log_level "INFO"
)
if [[ -n "$data_dir" ]]; then
  cmd+=(--data_dir "$data_dir")
fi

runner=(python3 benchmark_scripts/runner.py
  --stage multi_gpu
  --task infer
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --assets-manifest "$manifest"
  --decision-reason "kvpress evaluation/evaluate.py with --device auto (device_map='auto'), restricting visible devices to CUDA_VISIBLE_DEVICES=$gpus."
)
if [[ -n "$python_bin" ]]; then
  runner+=(--python "$python_bin")
fi
if [[ -n "$report_path" ]]; then
  runner+=(--report-path "$report_path")
fi
runner+=(--use-resolved-python -- "${cmd[@]}")

exec "${runner[@]}"

