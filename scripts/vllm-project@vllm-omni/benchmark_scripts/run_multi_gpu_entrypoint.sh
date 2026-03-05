#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU execution using the repository's native entrypoint (1 step).

Outputs (always):
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Optional:
  --python <path>                 Explicit python executable to use
  --report-path <path>            Override report.json path (default: /opt/scimlopsbench/report.json)
  --cuda-visible-devices <list>   Default: 0,1
  --timeout-sec <n>               Default: 1200
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin=""
report_path=""
cuda_visible_devices="0,1"
timeout_sec="1200"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_bin="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --cuda-visible-devices)
      cuda_visible_devices="${2:-}"; shift 2 ;;
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

out_dir="build_output/multi_gpu"
mkdir -p "$out_dir"
log_path="$out_dir/log.txt"
results_json="$out_dir/results.json"

exec > >(tee -a "$log_path") 2>&1

runner_py=""
if command -v python3 >/dev/null 2>&1; then
  runner_py="python3"
elif command -v python >/dev/null 2>&1; then
  runner_py="python"
else
  echo "No python found to run runner.py" >&2
  exit 1
fi

prepare_results="build_output/prepare/results.json"
if [[ ! -f "$prepare_results" ]]; then
  "$runner_py" benchmark_scripts/runner.py \
    --stage multi_gpu \
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
    --stage multi_gpu \
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
    --stage multi_gpu \
    --task infer \
    --framework pytorch \
    --timeout-sec "$timeout_sec" \
    --decision-reason "Dataset path missing/invalid from $prepare_results: $dataset_path" \
    --failure-category data \
    -- bash -lc "echo 'Invalid dataset path: $dataset_path' && exit 1"
  exit 1
fi

prompt="$(head -n 1 "$dataset_path" | tr -d '\r' || true)"

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

if [[ -z "$python_bin" ]]; then
  echo "[multi_gpu] missing python (provide --python or valid report.json python_path)"
  cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": $timeout_sec,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "$dataset_path", "source": "", "version": "", "sha256": ""},
    "model": {"path": "$model_path", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {"CUDA_VISIBLE_DEVICES": "$cuda_visible_devices"},
    "decision_reason": "python_path missing"
  },
  "failure_category": "missing_report",
  "error_excerpt": "$(tail -n 220 "$log_path" | sed 's/\\/\\\\/g; s/\"/\\\"/g')"
}
JSON
  exit 1
fi

# Ensure a deterministic visible set for detection and execution.
export CUDA_VISIBLE_DEVICES="$cuda_visible_devices"

# Detect GPU count using the resolved python (torch preferred).
gpu_count="$("$python_bin" - <<'PY' 2>/dev/null || true
try:
    import torch
    print(torch.cuda.device_count())
except Exception:
    print(-1)
PY
)"

if [[ -z "$gpu_count" ]]; then
  gpu_count="-1"
fi

echo "[multi_gpu] detected_gpu_count=$gpu_count"

requested_count=0
IFS=',' read -r -a req_gpus <<<"$cuda_visible_devices"
for g in "${req_gpus[@]}"; do
  [[ -n "${g// /}" ]] && requested_count=$((requested_count+1))
done

if [[ "$gpu_count" -lt 2 || "$requested_count" -lt 2 ]]; then
  echo "[multi_gpu] insufficient hardware: need >=2 GPUs (detected=$gpu_count requested_visible=$requested_count)"
  cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "insufficient_hardware",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": $timeout_sec,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "$dataset_path", "source": "", "version": "", "sha256": ""},
    "model": {"path": "$model_path", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "$python_bin",
    "git_commit": "",
    "env_vars": {"CUDA_VISIBLE_DEVICES": "$cuda_visible_devices"},
    "decision_reason": "Multi-GPU requires >=2 visible GPUs."
  },
  "failure_category": "runtime",
  "error_excerpt": "$(tail -n 220 "$log_path" | sed 's/\\/\\\\/g; s/\"/\\\"/g')"
}
JSON
  exit 1
fi

# vLLM-Omni requires the upstream `vllm` package, but it is not pinned as a hard
# dependency in pyproject.toml. Ensure it exists before running the entrypoint.
preflight_log="$out_dir/vllm_preflight.txt"

vllm_spec="${SCIMLOPSBENCH_VLLM_SPEC:-vllm==0.12.0}"
if [[ -z "${SCIMLOPSBENCH_VLLM_SPEC:-}" && -n "${VLLM_PRECOMPILED_WHEEL_LOCATION:-}" ]]; then
  vllm_spec="$VLLM_PRECOMPILED_WHEEL_LOCATION"
fi

if ! "$python_bin" -c "import vllm" >/dev/null 2>&1; then
  if [[ "${SCIMLOPSBENCH_OFFLINE:-}" == "1" || "${HF_HUB_OFFLINE:-}" == "1" ]]; then
    {
      echo "[multi_gpu] vllm is required but not importable."
      echo "[multi_gpu] Offline mode set; not attempting install."
      echo "[multi_gpu] Install vllm per docs/getting_started/quickstart.md or set SCIMLOPSBENCH_VLLM_SPEC / VLLM_PRECOMPILED_WHEEL_LOCATION."
    } >"$preflight_log"
  else
    export PIP_CACHE_DIR="$repo_root/benchmark_assets/cache/pip"
    export PIP_DISABLE_PIP_VERSION_CHECK=1
    mkdir -p "$PIP_CACHE_DIR"
    {
      echo "[multi_gpu] vllm not importable; attempting install."
      echo "[multi_gpu] python=$python_bin"
      echo "[multi_gpu] install_command: $python_bin -m pip install -q $vllm_spec"
      "$python_bin" -m pip --version
      "$python_bin" -m pip install -q "$vllm_spec"
      echo "[multi_gpu] post_install_import_check:"
      "$python_bin" -c "import vllm; print(getattr(vllm, '__version__', ''))"
    } >"$preflight_log" 2>&1 || true
  fi

  if ! "$python_bin" -c "import vllm" >/dev/null 2>&1; then
    "$runner_py" benchmark_scripts/runner.py \
      --stage multi_gpu \
      --task infer \
      --framework pytorch \
      --timeout-sec "$timeout_sec" \
      --python "$python_bin" \
      --decision-reason "Missing required dependency: vllm (see $preflight_log). vllm is required by vllm_omni/config/model.py and docs/getting_started/quickstart.md." \
      --failure-category deps \
      -- bash -lc "echo '[multi_gpu] vllm import failed'; echo '--- preflight ---'; cat '$preflight_log'; exit 1"
    exit 1
  fi
fi

export HF_HOME="$repo_root/benchmark_assets/cache/hf_home"
export HF_HUB_CACHE="$HF_HOME/hub"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export DIFFUSERS_CACHE="$HF_HOME/diffusers"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export TMPDIR="$repo_root/build_output/multi_gpu/tmp"
mkdir -p "$TMPDIR"

out_png="build_output/multi_gpu/output.png"
mkdir -p "build_output/multi_gpu"

decision_reason="Official entrypoint examples/offline_inference/text_to_image/text_to_image.py with batch_size=1 (single prompt), steps=1 (num_inference_steps=1), multi-GPU via DiffusionParallelConfig (--ulysses_degree=$requested_count) and CUDA_VISIBLE_DEVICES=$cuda_visible_devices."

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
  --ulysses_degree "$requested_count"
)

"$runner_py" benchmark_scripts/runner.py \
  --stage multi_gpu \
  --task infer \
  --framework pytorch \
  --timeout-sec "$timeout_sec" \
  --python "$python_bin" \
  --decision-reason "$decision_reason" \
  -- "${cmd[@]}"
