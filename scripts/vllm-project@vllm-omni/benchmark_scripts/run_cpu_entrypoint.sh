#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal CPU execution using the repository's native entrypoint (1 step).

This repo is GPU/NPU-focused; this stage may be skipped when CPU is not supported.

Outputs (always):
  build_output/cpu/log.txt
  build_output/cpu/results.json

Optional:
  --python <path>            Explicit python executable to use
  --report-path <path>       Override report.json path (default: /opt/scimlopsbench/report.json)
  --timeout-sec <n>          Default: 600
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin=""
report_path=""
timeout_sec="600"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_bin="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --timeout-sec)
      timeout_sec="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

# Best-effort evidence check for CPU support.
cpu_not_supported_evidence=0
evidence_notes=()

if [[ -f "docs/getting_started/installation/README.md" ]]; then
  if grep -q "supports the following hardware platforms" docs/getting_started/installation/README.md 2>/dev/null; then
    if grep -q "\\[GPU\\]" docs/getting_started/installation/README.md 2>/dev/null && grep -q "\\[NPU\\]" docs/getting_started/installation/README.md 2>/dev/null; then
      evidence_notes+=("docs/getting_started/installation/README.md lists GPU/NPU only")
    fi
  fi
fi

if [[ -f "vllm_omni/diffusion/worker/gpu_worker.py" ]]; then
  if grep -q "torch\\.device(f\\\"cuda" vllm_omni/diffusion/worker/gpu_worker.py 2>/dev/null && grep -q "torch\\.cuda\\.set_device" vllm_omni/diffusion/worker/gpu_worker.py 2>/dev/null; then
    evidence_notes+=("vllm_omni/diffusion/worker/gpu_worker.py hardcodes CUDA device usage")
  fi
fi

if [[ "${#evidence_notes[@]}" -ge 2 ]]; then
  cpu_not_supported_evidence=1
fi

decision_reason="CPU run uses official offline inference entrypoint examples/offline_inference/text_to_image/text_to_image.py with CUDA disabled; skipped only if strong repo evidence suggests CPU is not supported."
if [[ "$cpu_not_supported_evidence" -eq 1 ]]; then
  decision_reason="Skipping CPU stage (repo_not_supported): ${evidence_notes[*]}"
fi

runner_py=""
if command -v python3 >/dev/null 2>&1; then
  runner_py="python3"
elif command -v python >/dev/null 2>&1; then
  runner_py="python"
else
  echo "No python found to run runner.py" >&2
  exit 1
fi

if [[ "$cpu_not_supported_evidence" -eq 1 ]]; then
  "$runner_py" benchmark_scripts/runner.py \
    --stage cpu \
    --task infer \
    --framework pytorch \
    --timeout-sec "$timeout_sec" \
    --decision-reason "$decision_reason" \
    --skip \
    --skip-reason repo_not_supported
  exit 0
fi

# Otherwise, attempt a best-effort CPU run by disabling CUDA visibility.
export CUDA_VISIBLE_DEVICES=""
export HF_HOME="$repo_root/benchmark_assets/cache/hf_home"
export HF_HUB_CACHE="$HF_HOME/hub"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export DIFFUSERS_CACHE="$HF_HOME/diffusers"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export TMPDIR="$repo_root/build_output/cpu/tmp"
mkdir -p "$TMPDIR"

prepare_results="build_output/prepare/results.json"
if [[ ! -f "$prepare_results" ]]; then
  "$runner_py" benchmark_scripts/runner.py \
    --stage cpu \
    --task infer \
    --framework pytorch \
    --timeout-sec "$timeout_sec" \
    --decision-reason "prepare stage results missing at $prepare_results" \
    --failure-category data \
    -- bash -lc "echo 'Missing $prepare_results' && exit 1"
  exit 1
fi

model_path="$("$runner_py" - "$prepare_results" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print((data.get("assets") or {}).get("model", {}).get("path", "") or "")
except Exception:
    pass
PY
)"
dataset_path="$("$runner_py" - "$prepare_results" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print((data.get("assets") or {}).get("dataset", {}).get("path", "") or "")
except Exception:
    pass
PY
)"

if [[ -z "$model_path" || ! -d "$model_path" ]]; then
  "$runner_py" benchmark_scripts/runner.py \
    --stage cpu \
    --task infer \
    --framework pytorch \
    --timeout-sec "$timeout_sec" \
    --decision-reason "Model path missing/invalid from $prepare_results: $model_path" \
    --failure-category model \
    -- bash -lc "echo 'Invalid model path: $model_path' && exit 1"
  exit 1
fi
if [[ -z "$dataset_path" || ! -f "$dataset_path" ]]; then
  "$runner_py" benchmark_scripts/runner.py \
    --stage cpu \
    --task infer \
    --framework pytorch \
    --timeout-sec "$timeout_sec" \
    --decision-reason "Dataset path missing/invalid from $prepare_results: $dataset_path" \
    --failure-category data \
    -- bash -lc "echo 'Invalid dataset path: $dataset_path' && exit 1"
  exit 1
fi

prompt="$(head -n 1 "$dataset_path" | tr -d '\r' || true)"
out_png="build_output/cpu/output.png"
mkdir -p "build_output/cpu"

cmd=( "{python}" examples/offline_inference/text_to_image/text_to_image.py
  --model "$model_path"
  --prompt "$prompt"
  --seed 42
  --cfg_scale 4.0
  --num_images_per_prompt 1
  --num_inference_steps 1
  --height 64
  --width 64
  --output "$out_png"
)

if [[ -z "$python_bin" ]]; then
  report_path_resolved="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
  python_bin="$("$runner_py" - "$report_path_resolved" <<'PY' 2>/dev/null || true
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
if not p.exists():
    sys.exit(2)
data = json.loads(p.read_text(encoding="utf-8"))
val = data.get("python_path")
if isinstance(val, str) and val:
    print(val)
PY
)"
fi

"$runner_py" benchmark_scripts/runner.py \
  --stage cpu \
  --task infer \
  --framework pytorch \
  --timeout-sec "$timeout_sec" \
  ${python_bin:+--python "$python_bin"} \
  --decision-reason "$decision_reason" \
  -- "${cmd[@]}"

