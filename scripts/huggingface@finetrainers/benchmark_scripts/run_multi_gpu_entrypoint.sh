#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step multi-GPU training run via the repository entrypoint (train.py), using torchrun/torch.distributed.run.

Outputs (fixed):
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Options:
  --report-path <path>   Agent report JSON (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  --python <path>        Explicit python to use (overrides report)
  --out-dir <path>       Root output dir (default: build_output)
  --timeout-sec <n>      Default: 1200
  --gpu-ids <csv>        Default: 0,1  (also supports env BENCH_MULTI_GPU_IDS)

Behavior:
  - If detected GPU count < 2, writes results.json and exits 1.
EOF
}

report_path=""
python_override=""
out_root="build_output"
timeout_sec="1200"
gpu_ids="${BENCH_MULTI_GPU_IDS:-0,1}"

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
    --gpu-ids)
      gpu_ids="${2:-}"; shift 2 ;;
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
stage_dir="$out_root_abs/multi_gpu"
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
  "stage": "multi_gpu",
  "task": "train",
  "command": "python (not found)",
  "timeout_sec": 1200,
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
  "stage":"multi_gpu",
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

resolve_python_args=()
[[ -n "$report_path" ]] && resolve_python_args+=(--report-path "$report_path")
[[ -n "$python_override" ]] && resolve_python_args+=(--python "$python_override")

if ! resolved_python="$("$sys_python" benchmark_scripts/runner.py --print-python "${resolve_python_args[@]}")"; then
  echo "Failed to resolve python via agent report; provide --python." >"$stage_dir/log.txt"
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
  "stage":"multi_gpu",
  "task":"train",
  "command":"resolve_python(report.json)",
  "timeout_sec": int(${timeout_sec}),
  "framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{
    "python":"",
    "git_commit": git_commit(),
    "env_vars": {k:os.environ.get(k) for k in ["SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON"] if os.environ.get(k)},
    "decision_reason":"missing/invalid agent report; cannot resolve python_path"
  },
  "failure_category":"missing_report",
  "error_excerpt":"\\n".join(log_path.read_text(errors='replace').splitlines()[-220:]) if log_path.exists() else ""
}
(stage_dir / "results.json").write_text(json.dumps(payload, indent=2) + "\\n")
PY
  exit 1
fi

# Detect GPU count using the resolved python.
gpu_count="$("$resolved_python" - <<'PY' 2>/dev/null || echo 0
import torch
print(torch.cuda.device_count())
PY
)"

if ! [[ "$gpu_count" =~ ^[0-9]+$ ]]; then
  gpu_count=0
fi

if [[ "$gpu_count" -lt 2 ]]; then
  echo "Need >=2 GPUs for multi-GPU stage; detected gpu_count=$gpu_count" >"$stage_dir/log.txt"
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
  "skip_reason":"insufficient_hardware",
  "exit_code":1,
  "stage":"multi_gpu",
  "task":"train",
  "command":"gpu_count_check",
  "timeout_sec": int(${timeout_sec}),
  "framework":"pytorch",
  "assets":{"dataset":{"path": ${dataset_config@Q}, "source":"", "version":"", "sha256":""},"model":{"path": ${model_dir@Q}, "source":"", "version":"", "sha256":""}},
  "meta":{
    "python": ${resolved_python@Q},
    "git_commit": git_commit(),
    "env_vars": {k:os.environ.get(k) for k in ["CUDA_VISIBLE_DEVICES","SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON"] if os.environ.get(k)},
    "decision_reason": "Detected <2 GPUs; cannot run multi-GPU entrypoint.",
    "detected_gpu_count": int(${gpu_count}),
  },
  "failure_category":"insufficient_hardware",
  "error_excerpt":"\\n".join(log_path.read_text(errors='replace').splitlines()[-220:]) if log_path.exists() else ""
}
(stage_dir / "results.json").write_text(json.dumps(payload, indent=2) + "\\n")
PY
  exit 1
fi

# Compute world size from provided GPU ids.
IFS=',' read -r -a gpu_arr <<<"$gpu_ids"
world_size="${#gpu_arr[@]}"
if [[ "$world_size" -lt 2 ]]; then
  echo "gpu_ids=$gpu_ids does not include >=2 GPUs" >"$stage_dir/log.txt"
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

decision_reason="Native entrypoint train.py; multi-GPU launch via torch.distributed.run (torchrun equivalent) with parallel_backend=ptd and dp_degree=world_size; LoRA target_modules derived from examples for model_name=$model_name."

runner_args=(--stage multi_gpu --task train --framework pytorch --timeout-sec "$timeout_sec" --decision-reason "$decision_reason")
[[ -n "$report_path" ]] && runner_args+=(--report-path "$report_path")
[[ -n "$python_override" ]] && runner_args+=(--python "$python_override")

"$sys_python" benchmark_scripts/runner.py "${runner_args[@]}" \
  "${ld_env_args[@]}" \
  --env "CUDA_VISIBLE_DEVICES=$gpu_ids" \
  --fail-regex "An error occurred during training:" \
  --fail-regex "Traceback \\(most recent call last\\):" \
  -- \
  "{python}" -m torch.distributed.run \
    --standalone \
    --nnodes=1 \
    --nproc_per_node="$world_size" \
    train.py \
      --parallel_backend ptd \
      --pp_degree 1 \
      --dp_degree "$world_size" \
      --dp_shards 1 \
      --cp_degree 1 \
      --tp_degree 1 \
      --model_name "$model_name" \
      --pretrained_model_name_or_path "$model_dir" \
      --dataset_config "$dataset_config" \
      --training_type lora \
      "${target_modules_args[@]}" \
      --batch_size 1 \
      --train_steps 1 \
      --max_data_samples 1 \
      --checkpointing_steps 0 \
      --init_timeout 600 \
      --nccl_timeout 600 \
      --report_to none \
      --output_dir "$stage_dir/run_output" \
      --logging_dir "$stage_dir/logging"

exit $?
