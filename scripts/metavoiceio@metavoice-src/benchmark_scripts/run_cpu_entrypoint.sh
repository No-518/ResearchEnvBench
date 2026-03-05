#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal CPU inference through the repository's native entrypoint.

Outputs:
  build_output/cpu/log.txt
  build_output/cpu/results.json

Options:
  --python <path>        Override python executable (otherwise resolved from /opt/scimlopsbench/report.json)
  --report-path <path>   Override report path (default: /opt/scimlopsbench/report.json)
  --timeout-sec <n>      Default: 600
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
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

stage="cpu"
out_dir="$repo_root/build_output/$stage"
mkdir -p "$out_dir"

prepare_results="$repo_root/build_output/prepare/results.json"

dataset_dir="$repo_root/benchmark_assets/dataset"
spk_ref="$dataset_dir/bria.mp3"

model_id="$(
  PREP="$prepare_results" python3 - <<'PY' 2>/dev/null || true
import json, os, pathlib
p = pathlib.Path(os.environ["PREP"]).resolve()
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print((data.get("assets", {}).get("model", {}) or {}).get("source", "") or "")
except Exception:
    print("")
PY
)"
model_id="${model_id:-metavoiceio/metavoice-1B-v0.1}"

entrypoint_script="fam/llm/inference.py"
output_dir_rel="build_output/cpu/outputs"
mkdir -p "$repo_root/$output_dir_rel"

decision_reason="Using native entrypoint fam/llm/inference.py for minimal inference (num_samples=1, max_new_tokens=1) on CPU."

env_root="$repo_root/benchmark_assets/cache"
home_dir="$env_root/home_cpu"
xdg_cache="$env_root/xdg"
hf_home="$env_root/huggingface"
torch_home="$env_root/torch"
tmpdir="$repo_root/build_output/cpu/tmp"
mkdir -p "$home_dir" "$xdg_cache" "$hf_home" "$torch_home" "$tmpdir"

cmd_str="<python> $entrypoint_script --spk_cond_path $spk_ref --text Hello --huggingface_repo_id $model_id --num_samples 1 --max_new_tokens 1 --device cpu --dtype float32 --output_dir $output_dir_rel"

if [[ ! -f "$entrypoint_script" ]]; then
  runner=(python3 "$repo_root/benchmark_scripts/runner.py"
    --stage "$stage" --task infer --framework pytorch --timeout-sec "$timeout_sec" --out-dir "$out_dir"
    --assets-from "$prepare_results"
    --no-run --status failure --failure-category entrypoint_not_found --command-str "$cmd_str"
    --decision-reason "Missing entrypoint script: $entrypoint_script"
  )
  "${runner[@]}"
  exit 1
fi

if [[ ! -f "$spk_ref" ]]; then
  runner=(python3 "$repo_root/benchmark_scripts/runner.py"
    --stage "$stage" --task infer --framework pytorch --timeout-sec "$timeout_sec" --out-dir "$out_dir"
    --assets-from "$prepare_results"
    --require-python
  )
  [[ -n "$python_bin" ]] && runner+=(--python "$python_bin")
  [[ -n "$report_path" ]] && runner+=(--report-path "$report_path")
  runner+=(
    --no-run --status failure --failure-category data --command-str "$cmd_str"
    --decision-reason "Missing speaker reference audio at $spk_ref; run prepare_assets.sh first."
  )
  "${runner[@]}"
  exit 1
fi

runner=(python3 "$repo_root/benchmark_scripts/runner.py"
  --stage "$stage" --task infer --framework pytorch --timeout-sec "$timeout_sec" --out-dir "$out_dir"
  --assets-from "$prepare_results"
  --require-python
)
[[ -n "$python_bin" ]] && runner+=(--python "$python_bin")
[[ -n "$report_path" ]] && runner+=(--report-path "$report_path")
runner+=(
  --env "HOME=$home_dir"
  --env "XDG_CACHE_HOME=$xdg_cache"
  --env "HF_HOME=$hf_home"
  --env "HF_HUB_DISABLE_TELEMETRY=1"
  --env "HF_HUB_OFFLINE=0"
  --env "TORCH_HOME=$torch_home"
  --env "TMPDIR=$tmpdir"
  --env "CUDA_VISIBLE_DEVICES=0"
  --python-script "$entrypoint_script"
  --decision-reason "$decision_reason"
  --
  --spk_cond_path "$spk_ref"
  --text "Hello"
  --huggingface_repo_id "$model_id"
  --num_samples 1
  --max_new_tokens 1
  --device cpu
  --dtype float32
  --output_dir "$output_dir_rel"
)
("${runner[@]}")
