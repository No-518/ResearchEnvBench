#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step multi-GPU training run using the repository entrypoint (python -m lcm.train) via torchrun.

Writes:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Options:
  --python <path>          Python executable to use (recommended).
  --report-path <path>     Override report.json path used by runner (default: /opt/scimlopsbench/report.json).
  --assets-from <path>     Prepare stage results.json (default: build_output/prepare/results.json).
  --timeout-sec <int>      Default: 1200
  --devices <csv>          CUDA_VISIBLE_DEVICES list (default: 0,1)
EOF
}

python_bin=""
report_path=""
assets_from="build_output/prepare/results.json"
timeout_sec="1200"
devices="${SCIMLOPSBENCH_MULTI_GPU_DEVICES:-0,1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --assets-from) assets_from="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    --devices) devices="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/multi_gpu"
mkdir -p "$stage_dir"

runner_py="${python_bin:-${SCIMLOPSBENCH_PYTHON:-python3}}"

dataset_path=""
if [[ -f "$repo_root/$assets_from" ]]; then
  dataset_path="$(python3 - <<PY 2>/dev/null || true
import json
from pathlib import Path
p = Path(${repo_root@Q}) / Path(${assets_from@Q})
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("assets",{}).get("dataset",{}).get("path",""))
except Exception:
    print("")
PY
)"
fi

if [[ -z "$dataset_path" || ! -d "$dataset_path" ]]; then
  {
    echo "[multi_gpu] ERROR: dataset path missing/invalid. Expected build_output/prepare/results.json with assets.dataset.path"
    echo "[multi_gpu] assets_from=$assets_from"
    echo "[multi_gpu] dataset_path=$dataset_path"
  } >"$stage_dir/log.txt"
  RESULTS_JSON_PATH="$stage_dir/results.json" \
    LOG_PATH="$stage_dir/log.txt" \
    PYTHON_BIN="$python_bin" \
    DATASET_PATH="$dataset_path" \
    DEVICES="$devices" \
    GIT_COMMIT="$(cd "$repo_root" && git rev-parse HEAD 2>/dev/null || true)" \
    python3 - <<'PY'
import json
import os
from pathlib import Path

def tail_file(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:]) if len(lines) > max_lines else "\n".join(lines)

results_path = Path(os.environ["RESULTS_JSON_PATH"])
log_path = Path(os.environ.get("LOG_PATH", ""))

payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "multi_gpu",
    "task": "train",
    "command": "bash benchmark_scripts/run_multi_gpu_entrypoint.sh",
    "timeout_sec": 1200,
    "framework": "pytorch",
    "assets": {
        "dataset": {"path": os.environ.get("DATASET_PATH", ""), "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PYTHON_BIN", ""),
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("DEVICES", "")},
        "decision_reason": "Cannot run lcm.train without prepared assets.",
    },
    "failure_category": "data",
    "error_excerpt": tail_file(log_path),
}

results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
  exit 1
fi

export CUDA_VISIBLE_DEVICES="$devices"

gpu_count="$("$runner_py" - <<'PY' 2>/dev/null || true
try:
    import torch
    print(torch.cuda.device_count() if torch.cuda.is_available() else 0)
except Exception:
    print(0)
PY
)"

IFS=',' read -r -a dev_arr <<<"$devices"
nproc="${#dev_arr[@]}"

decision_reason="Use README distributed launch (torchrun) for -m lcm.train with +pretrain=mse; enforce max_steps=1 and batch_size=1; use DDP (use_fsdp=false) across $nproc GPUs (CUDA_VISIBLE_DEVICES=$devices)."

cmd=(
  "$runner_py" "benchmark_scripts/runner.py"
  --stage multi_gpu
  --task train
  --framework pytorch
  --timeout-sec "$timeout_sec"
  --assets-from "$assets_from"
  --decision-reason "$decision_reason"
)

if [[ -n "$python_bin" ]]; then
  cmd+=(--python "$python_bin")
fi
if [[ -n "$report_path" ]]; then
  cmd+=(--report-path "$report_path")
fi

if [[ "${gpu_count:-0}" -lt 2 || "$nproc" -lt 2 ]]; then
  cmd+=(--failure-category insufficient_hardware --)
  cmd+=(bash -lc "echo '[multi_gpu] insufficient_hardware: need >=2 GPUs; observed gpu_count=${gpu_count:-0}, devices=$devices (CUDA_VISIBLE_DEVICES='\"$CUDA_VISIBLE_DEVICES\"')'; exit 1")
  exec "${cmd[@]}"
fi

train_out="$repo_root/build_output/multi_gpu/train_out"
hydra_dir="$repo_root/build_output/multi_gpu/hydra"
mkdir -p "$train_out" "$hydra_dir"

cmd+=(--)
cmd+=(
  "{python}" -m torch.distributed.run
  --standalone
  --nnodes=1
  "--nproc-per-node=$nproc"
  -m lcm.train
  launcher=standalone
  +pretrain=mse
  +trainer.use_submitit=false
  "hydra.run.dir=$hydra_dir"
  "++trainer.output_dir=$train_out"
  "++trainer.model_arch=toy_base_lcm"
  "++trainer.model_arch_overrides={sonar_normalizer_name:dummy_sonar_normalizer}"
  "++trainer.max_steps=1"
  "++trainer.use_fsdp=false"
  "++trainer.dtype=torch.float32"
  "++trainer.data_loading_config.batch_size=1"
  "++trainer.data_loading_config.max_tokens=0"
  "++trainer.validation_data_loading_config.batch_size=1"
  "++trainer.validation_data_loading_config.max_tokens=0"
  "++trainer.training_data[0].parquet_path=$dataset_path"
  "++trainer.validation_data[0].parquet_path=$dataset_path"
  "++trainer.validate_every_n_steps=1000000"
  "++trainer.checkpoint_every_n_steps=1000000"
  "++trainer.save_model_every_n_steps=1000000"
  "++trainer.publish_metrics_every_n_steps=1"
)

exec "${cmd[@]}"
