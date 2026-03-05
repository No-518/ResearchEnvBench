#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run TGATE repo entrypoint (main.py) for exactly 1 inference step on CPU.

This repo's main.py hardcodes `.to("cuda")` for all model branches; if so, this stage is marked skipped as repo_not_supported.

Outputs:
  build_output/cpu/log.txt
  build_output/cpu/results.json

Options:
  --report-path <path>   Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --python <path>        Explicit python executable (overrides report resolution)
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="$repo_root/build_output/cpu"
log_txt="$out_dir/log.txt"
results_json="$out_dir/results.json"
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_override=""
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
: >"$log_txt"

bootstrap_py="$(command -v python3 || command -v python || true)"
if [[ -z "$bootstrap_py" ]]; then
  echo "[cpu] python3/python not found in PATH" | tee -a "$log_txt" >&2
  cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "cpu",
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
    "env_vars": {"CUDA_VISIBLE_DEVICES": ""},
    "decision_reason": "python3/python not found in PATH; cannot run runner.py"
  },
  "failure_category": "deps",
  "error_excerpt": "python3/python not found in PATH"
}
JSON
  exit 1
fi

prepare_results="$repo_root/build_output/prepare/results.json"
selected_model="$("$bootstrap_py" - <<PY 2>/dev/null || true
import json
from pathlib import Path
p=Path("$prepare_results")
if p.is_file():
  d=json.loads(p.read_text(encoding="utf-8"))
  print(d.get("meta", {}).get("selected", {}).get("main_model_arg", ""))
PY
)"
selected_model="${selected_model:-}"
prompt_path="$("$bootstrap_py" - <<PY 2>/dev/null || true
import json
from pathlib import Path
p=Path("$prepare_results")
if p.is_file():
  d=json.loads(p.read_text(encoding="utf-8"))
  print(d.get("assets", {}).get("dataset", {}).get("path", ""))
PY
)"
prompt_path="${prompt_path:-}"
prompt=""
if [[ -n "$prompt_path" && -f "$prompt_path" ]]; then
  prompt="$(head -n 1 "$prompt_path" 2>/dev/null || true)"
fi

saved_path="$out_dir/generated"
cmd_preview="python main.py --model ${selected_model:-<missing>} --prompt '<prompt>' --saved_path '$saved_path' --inference_step 1"

cpu_not_supported=0
if [[ -f "$repo_root/main.py" ]]; then
  if grep -qE -- '\.to\(["'"'"']cuda["'"'"']\)' "$repo_root/main.py"; then
    if ! grep -qE -- '--device|--cpu|--no_cuda|--gpus|--accelerator' "$repo_root/main.py"; then
    cpu_not_supported=1
    fi
  fi
fi

if [[ "$cpu_not_supported" -eq 1 ]]; then
  echo "[cpu] Skipping: main.py hardcodes CUDA and has no CPU/device flag." | tee -a "$log_txt"
  "$bootstrap_py" "$repo_root/benchmark_scripts/runner.py" \
    --stage cpu --task infer --framework pytorch \
    --out-dir "$out_dir" \
    --report-path "$report_path" \
    ${python_override:+--python "$python_override"} \
    --skip --skip-reason repo_not_supported \
    --failure-category cpu_not_supported \
    --decision-reason "Evidence: main.py contains hard-coded .to(\"cuda\") in all model branches and exposes no CLI flag to select CPU; forcing CPU via CUDA_VISIBLE_DEVICES cannot work." \
    --command "$cmd_preview"
  exit 0
fi

echo "[cpu] Attempting CPU run via entrypoint (may fail if CUDA-only): $cmd_preview" | tee -a "$log_txt"

# If the repo ever adds CPU support, run it through runner.
"$bootstrap_py" "$repo_root/benchmark_scripts/runner.py" \
  --stage cpu --task infer --framework pytorch \
  --out-dir "$out_dir" \
  --report-path "$report_path" \
  ${python_override:+--python "$python_override"} \
  --requires-python \
  --assets-from "$prepare_results" \
  --decision-reason "Use repo entrypoint main.py for 1-step inference; force CPU via CUDA_VISIBLE_DEVICES=\"\"." \
  --env "CUDA_VISIBLE_DEVICES=" \
  --env "HF_HOME=$repo_root/benchmark_assets/cache/hf_home" \
  --env "HUGGINGFACE_HUB_CACHE=$repo_root/benchmark_assets/cache/hf_home/hub" \
  --env "TRANSFORMERS_CACHE=$repo_root/benchmark_assets/cache/hf_home/transformers" \
  --env "DIFFUSERS_CACHE=$repo_root/benchmark_assets/cache/hf_home/diffusers" \
  --env "HF_DATASETS_CACHE=$repo_root/benchmark_assets/cache/hf_home/datasets" \
  --env "XDG_CACHE_HOME=$repo_root/benchmark_assets/cache/xdg" \
  --env "TORCH_HOME=$repo_root/benchmark_assets/cache/torch" \
  --env "PIP_CACHE_DIR=$repo_root/benchmark_assets/cache/pip" \
  --python-script "main.py" -- \
    --model "$selected_model" \
    --prompt "$prompt" \
    --saved_path "$saved_path" \
    --inference_step 1 \
    --gate_step 1 \
    --sp_interval 1 \
    --fi_interval 1 \
    --warm_up 0
