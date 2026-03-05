#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal single-GPU execution using the repository's native entrypoint (1 step).

Outputs (always):
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

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
    --stage single_gpu \
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
    --stage single_gpu \
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
    --stage single_gpu \
    --task infer \
    --framework pytorch \
    --timeout-sec "$timeout_sec" \
    --decision-reason "Dataset path missing/invalid from $prepare_results: $dataset_path" \
    --failure-category data \
    -- bash -lc "echo 'Invalid dataset path: $dataset_path' && exit 1"
  exit 1
fi

prompt="$(head -n 1 "$dataset_path" | tr -d '\r' || true)"

export CUDA_VISIBLE_DEVICES="0"
export HF_HOME="$repo_root/benchmark_assets/cache/hf_home"
export HF_HUB_CACHE="$HF_HOME/hub"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export DIFFUSERS_CACHE="$HF_HOME/diffusers"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export TMPDIR="$repo_root/build_output/single_gpu/tmp"
mkdir -p "$TMPDIR"

out_png="build_output/single_gpu/output.png"
mkdir -p "build_output/single_gpu"

decision_reason="Official entrypoint examples/offline_inference/text_to_image/text_to_image.py with batch_size=1 (single prompt), steps=1 (num_inference_steps=1), CUDA_VISIBLE_DEVICES=0."

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
  "$runner_py" benchmark_scripts/runner.py \
    --stage single_gpu \
    --task infer \
    --framework pytorch \
    --timeout-sec "$timeout_sec" \
    --python-required \
    --decision-reason "python_path missing (provide --python or valid report.json python_path)" \
    -- bash -lc "echo '[single_gpu] missing python (provide --python or valid report.json python_path)' && exit 1"
  exit 1
fi

# vLLM-Omni requires the upstream `vllm` package, but it is not pinned as a hard
# dependency in pyproject.toml. Ensure it exists before running the entrypoint.
out_dir="build_output/single_gpu"
preflight_log="$out_dir/vllm_preflight.txt"
mkdir -p "$out_dir"

vllm_spec="${SCIMLOPSBENCH_VLLM_SPEC:-vllm==0.12.0}"
if [[ -z "${SCIMLOPSBENCH_VLLM_SPEC:-}" && -n "${VLLM_PRECOMPILED_WHEEL_LOCATION:-}" ]]; then
  vllm_spec="$VLLM_PRECOMPILED_WHEEL_LOCATION"
fi

if ! "$python_bin" -c "import vllm" >/dev/null 2>&1; then
  if [[ "${SCIMLOPSBENCH_OFFLINE:-}" == "1" || "${HF_HUB_OFFLINE:-}" == "1" ]]; then
    {
      echo "[single_gpu] vllm is required but not importable."
      echo "[single_gpu] Offline mode set; not attempting install."
      echo "[single_gpu] Install vllm per docs/getting_started/quickstart.md or set SCIMLOPSBENCH_VLLM_SPEC / VLLM_PRECOMPILED_WHEEL_LOCATION."
    } >"$preflight_log"
  else
    export PIP_CACHE_DIR="$repo_root/benchmark_assets/cache/pip"
    export PIP_DISABLE_PIP_VERSION_CHECK=1
    mkdir -p "$PIP_CACHE_DIR"
    {
      echo "[single_gpu] vllm not importable; attempting install."
      echo "[single_gpu] python=$python_bin"
      echo "[single_gpu] install_command: $python_bin -m pip install -q $vllm_spec"
      "$python_bin" -m pip --version
      "$python_bin" -m pip install -q "$vllm_spec"
      echo "[single_gpu] post_install_import_check:"
      "$python_bin" -c "import vllm; print(getattr(vllm, '__version__', ''))"
    } >"$preflight_log" 2>&1 || true
  fi

  if ! "$python_bin" -c "import vllm" >/dev/null 2>&1; then
    "$runner_py" benchmark_scripts/runner.py \
      --stage single_gpu \
      --task infer \
      --framework pytorch \
      --timeout-sec "$timeout_sec" \
      --python "$python_bin" \
      --decision-reason "Missing required dependency: vllm (see $preflight_log). vllm is required by vllm_omni/config/model.py and docs/getting_started/quickstart.md." \
      --failure-category deps \
      -- bash -lc "echo '[single_gpu] vllm import failed'; echo '--- preflight ---'; cat '$preflight_log'; exit 1"
    exit 1
  fi
fi

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

"$runner_py" benchmark_scripts/runner.py \
  --stage single_gpu \
  --task infer \
  --framework pytorch \
  --timeout-sec "$timeout_sec" \
  ${python_bin:+--python "$python_bin"} \
  --decision-reason "$decision_reason" \
  -- "${cmd[@]}"
