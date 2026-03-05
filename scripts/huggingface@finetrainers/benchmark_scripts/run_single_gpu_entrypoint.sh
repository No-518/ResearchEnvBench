#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step single-GPU training run via the repository entrypoint (train.py).

Outputs (fixed):
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

Options:
  --report-path <path>   Agent report JSON (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  --python <path>        Explicit python to use (overrides report)
  --out-dir <path>       Root output dir (default: build_output)
  --timeout-sec <n>      Default: 600
EOF
}

report_path=""
python_override=""
out_root="build_output"
timeout_sec="600"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --python)
      python_override="${2:-}"; shift 2 ;;
    --out-dir)
      out_root="${2:-}"; shift 2 ;;
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

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
out_root_abs="$(cd "$repo_root" && mkdir -p "$out_root" && cd "$out_root" && pwd)"
stage_dir="$out_root_abs/single_gpu"
mkdir -p "$stage_dir"

manifest_path="$repo_root/benchmark_assets/manifest.json"
dataset_config="$repo_root/benchmark_assets/dataset/dataset_config.json"
model_dir="$repo_root/benchmark_assets/model/current"

sys_python="$(command -v python3 || command -v python || true)"
if [[ -z "$sys_python" ]]; then
  echo "python not found on PATH" >"$stage_dir/log.txt"
  cat >"$stage_dir/results.json" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "single_gpu",
  "task": "train",
  "command": "python (not found)",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "No python found to run stage."},
  "failure_category": "deps",
  "error_excerpt": ""
}
JSON
  exit 1
fi

if [[ ! -f "$manifest_path" || ! -f "$dataset_config" || ! -e "$model_dir" ]]; then
  echo "Missing prepared assets. Expected:" >"$stage_dir/log.txt"
  echo "  $manifest_path" >>"$stage_dir/log.txt"
  echo "  $dataset_config" >>"$stage_dir/log.txt"
  echo "  $model_dir" >>"$stage_dir/log.txt"
  "$sys_python" - <<PY || true
import json, os, subprocess
from pathlib import Path
repo_root = Path(${repo_root@Q})
stage_dir = Path(${stage_dir@Q})
log_path = stage_dir / "log.txt"
def git_commit():
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""
payload = {
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"single_gpu",
  "task":"train",
  "command":"train.py (assets missing)",
  "timeout_sec": int(${timeout_sec}),
  "framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{
    "python":"",
    "git_commit": git_commit(),
    "env_vars": {k:os.environ.get(k) for k in ["SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON"] if os.environ.get(k)},
    "decision_reason":"prepare_assets.sh did not produce required manifest/dataset/model outputs"
  },
  "failure_category":"data",
  "error_excerpt":"\\n".join(log_path.read_text(errors='replace').splitlines()[-220:]) if log_path.exists() else ""
}
(stage_dir / "results.json").write_text(json.dumps(payload, indent=2) + "\\n")
PY
  exit 1
fi

# Constrain all caches/writes to new directories only.
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HOME="$repo_root/benchmark_assets/cache/hf_home"
export HF_HUB_CACHE="$HF_HOME/hub"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export HF_DATASETS_CACHE="$HF_HOME/datasets"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export WANDB_MODE="${WANDB_MODE:-offline}"
export WANDB_DIR="$stage_dir/wandb"
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export TORCH_NCCL_ENABLE_MONITORING="${TORCH_NCCL_ENABLE_MONITORING:-0}"

master_addr="127.0.0.1"
master_port="$("$sys_python" - <<'PY' 2>/dev/null || true
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
if [[ -z "$master_port" ]]; then
  master_port="29500"
fi

model_name="$("$sys_python" -c 'import json;print(json.load(open("benchmark_assets/manifest.json"))["model_name"])')"
ld_prefix="$("$sys_python" - <<'PY' 2>/dev/null || true
import json
from pathlib import Path
p = Path("benchmark_assets/manifest.json")
try:
    m = json.loads(p.read_text())
    deps = m.get("deps") or {}
    tc = deps.get("torchcodec") or {}
    ff = deps.get("ffmpeg") or {}
    v = (tc.get("ld_library_path_prefix") or ff.get("lib_dir") or "").strip()
    print(v)
except Exception:
    print("")
PY
)"

ld_env_args=()
if [[ -n "$ld_prefix" ]]; then
  if [[ -n "${LD_LIBRARY_PATH:-}" ]]; then
    ld_env_args=(--env "LD_LIBRARY_PATH=$ld_prefix:$LD_LIBRARY_PATH")
  else
    ld_env_args=(--env "LD_LIBRARY_PATH=$ld_prefix")
  fi
fi

target_modules="${BENCH_TARGET_MODULES:-}"
if [[ -z "$target_modules" ]]; then
  case "$model_name" in
    wan|wan_i2v)
      target_modules="blocks.*(to_q|to_k|to_v|to_out.0)" ;;
    cogview4)
      target_modules="transformer_blocks.*(to_q|to_k|to_v|to_out.0)" ;;
    flux_dev|hunyuan_video)
      target_modules="(transformer_blocks|single_transformer_blocks).*(to_q|to_k|to_v|to_out.0|add_q_proj|add_k_proj|add_v_proj|to_add_out)" ;;
    cogvideox|ltx_video)
      target_modules="(transformer_blocks|single_transformer_blocks).*(to_q|to_k|to_v|to_out.0)" ;;
    *)
      target_modules="" ;;
  esac
fi

target_modules_args=()
if [[ -n "$target_modules" ]]; then
  target_modules_args=(--target_modules "$target_modules")
fi

decision_reason="Native entrypoint train.py; 1 step on single GPU via CUDA_VISIBLE_DEVICES=0 + WORLD_SIZE=1; LoRA target_modules derived from examples for model_name=$model_name."
runner_args=(--stage single_gpu --task train --framework pytorch --timeout-sec "$timeout_sec" --decision-reason "$decision_reason")
[[ -n "$report_path" ]] && runner_args+=(--report-path "$report_path")
[[ -n "$python_override" ]] && runner_args+=(--python "$python_override")

"$sys_python" benchmark_scripts/runner.py "${runner_args[@]}" \
  "${ld_env_args[@]}" \
  --env "CUDA_VISIBLE_DEVICES=0" \
  --env "WORLD_SIZE=1" \
  --env "RANK=0" \
  --env "LOCAL_RANK=0" \
  --env "LOCAL_WORLD_SIZE=1" \
  --env "MASTER_ADDR=$master_addr" \
  --env "MASTER_PORT=$master_port" \
  --fail-regex "An error occurred during training:" \
  --fail-regex "Traceback \\(most recent call last\\):" \
  -- \
  "{python}" train.py \
    --parallel_backend accelerate \
    --model_name "$model_name" \
    --pretrained_model_name_or_path "$model_dir" \
    --dataset_config "$dataset_config" \
    --training_type lora \
    "${target_modules_args[@]}" \
    --batch_size 1 \
    --train_steps 1 \
    --max_data_samples 1 \
    --checkpointing_steps 0 \
    --report_to none \
    --output_dir "$stage_dir/run_output" \
    --logging_dir "$stage_dir/logging"

exit $?
