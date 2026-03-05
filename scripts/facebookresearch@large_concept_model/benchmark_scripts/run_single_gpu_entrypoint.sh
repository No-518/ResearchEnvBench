#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step single-GPU training run using the repository entrypoint (python -m lcm.train).

Writes:
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

Options:
  --python <path>          Python executable to use (recommended).
  --report-path <path>     Override report.json path used by runner (default: /opt/scimlopsbench/report.json).
  --assets-from <path>     Prepare stage results.json (default: build_output/prepare/results.json).
  --timeout-sec <int>      Default: 600
EOF
}

python_bin=""
report_path=""
assets_from="build_output/prepare/results.json"
timeout_sec="600"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --assets-from) assets_from="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/single_gpu"
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
    echo "[single_gpu] ERROR: dataset path missing/invalid. Expected build_output/prepare/results.json with assets.dataset.path"
    echo "[single_gpu] assets_from=$assets_from"
    echo "[single_gpu] dataset_path=$dataset_path"
  } >"$stage_dir/log.txt"
  RESULTS_JSON_PATH="$stage_dir/results.json" \
    LOG_PATH="$stage_dir/log.txt" \
    PYTHON_BIN="$python_bin" \
    DATASET_PATH="$dataset_path" \
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
    "stage": "single_gpu",
    "task": "train",
    "command": "bash benchmark_scripts/run_single_gpu_entrypoint.sh",
    "timeout_sec": 600,
    "framework": "pytorch",
    "assets": {
        "dataset": {"path": os.environ.get("DATASET_PATH", ""), "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": os.environ.get("PYTHON_BIN", ""),
        "git_commit": os.environ.get("GIT_COMMIT", ""),
        "env_vars": {"CUDA_VISIBLE_DEVICES": "0"},
        "decision_reason": "Cannot run lcm.train without prepared assets.",
    },
    "failure_category": "data",
    "error_excerpt": tail_file(log_path),
}

results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
  exit 1
fi

export CUDA_VISIBLE_DEVICES="0"

cuda_ok="$("$runner_py" - <<'PY' 2>/dev/null || true
try:
    import torch
    print("1" if torch.cuda.is_available() and torch.cuda.device_count() >= 1 else "0")
except Exception:
    print("0")
PY
)"

decision_reason="Use README entrypoint python -m lcm.train with +pretrain=mse; force single GPU via CUDA_VISIBLE_DEVICES=0 and ++trainer.fake_gang_device=cuda:0; enforce max_steps=1 and batch_size=1."

cmd=(
  "$runner_py" "benchmark_scripts/runner.py"
  --stage single_gpu
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

if [[ "$cuda_ok" != "1" ]]; then
  cmd+=(--failure-category insufficient_hardware --)
  cmd+=(bash -lc "echo '[single_gpu] insufficient_hardware: torch.cuda.is_available() is false or no visible GPUs (CUDA_VISIBLE_DEVICES='\"$CUDA_VISIBLE_DEVICES\"')'; exit 1")
  exec "${cmd[@]}"
fi

train_out="$repo_root/build_output/single_gpu/train_out"
hydra_dir="$repo_root/build_output/single_gpu/hydra"
mkdir -p "$train_out" "$hydra_dir"

cmd+=(--)
cmd+=(
  "{python}" -m lcm.train
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
  "++trainer.fake_gang_device=cuda:0"
)

exec "${cmd[@]}"
