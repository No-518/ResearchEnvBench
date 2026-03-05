#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU distributed training via repository entrypoint.

Defaults:
  - Requires >=2 visible GPUs; otherwise writes results.json and exits 1
  - Uses CUDA_VISIBLE_DEVICES=0,1 (override with --cuda-visible-devices)
  - Uses accelerate launch (python -m accelerate.commands.launch)
  - Uses assets from build_output/prepare/results.json (dataset)

Optional:
  --python <path>              Explicit python executable to use (highest priority)
  --report-path <path>         Agent report.json path override
  --timeout-sec <int>          Default: 1200
  --cuda-visible-devices <s>   Default: 0,1 (e.g. 0,1 or 2,3)
  --num-processes <int>        Default: 2
EOF
}

python_bin=""
report_path=""
timeout_sec="1200"
cuda_visible_devices="0,1"
num_processes="2"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    --cuda-visible-devices) cuda_visible_devices="${2:-}"; shift 2 ;;
    --num-processes) num_processes="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

BOOTSTRAP_PY="$(command -v python >/dev/null 2>&1 && echo python || echo python3)"

out_dir="build_output/multi_gpu"
log_path="$out_dir/log.txt"
results_json="$out_dir/results.json"
mkdir -p "$out_dir"

timestamp_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
git_commit="$(git rev-parse HEAD 2>/dev/null || true)"

prepare_results="build_output/prepare/results.json"

write_failure_results() {
  local failure_category="$1"
  local skip_reason="$2"
  local message="$3"
  local command_str="$4"
  local python_exe="$5"
  local python_source="$6"

  printf "%s\n" "$message" > "$log_path"
  BENCH_MGPU_REPO_ROOT="$repo_root" \
  BENCH_MGPU_RESULTS_JSON="$results_json" \
  BENCH_MGPU_PREPARE_RESULTS="$prepare_results" \
  BENCH_MGPU_SKIP_REASON="$skip_reason" \
  BENCH_MGPU_FAILURE_CATEGORY="$failure_category" \
  BENCH_MGPU_MESSAGE="$message" \
  BENCH_MGPU_COMMAND="$command_str" \
  BENCH_MGPU_TIMEOUT_SEC="$timeout_sec" \
  BENCH_MGPU_PYTHON_EXE="$python_exe" \
  BENCH_MGPU_PYTHON_SOURCE="$python_source" \
  BENCH_MGPU_GIT_COMMIT="$git_commit" \
  BENCH_MGPU_CUDA_VISIBLE_DEVICES="$cuda_visible_devices" \
  BENCH_MGPU_TIMESTAMP_UTC="$timestamp_utc" \
  "$BOOTSTRAP_PY" - <<'PY'
import json
import os
from pathlib import Path

repo_root = Path(os.environ["BENCH_MGPU_REPO_ROOT"]).resolve()
out = repo_root / os.environ["BENCH_MGPU_RESULTS_JSON"]
prepare_path = repo_root / os.environ["BENCH_MGPU_PREPARE_RESULTS"]

assets = {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
}
try:
    assets = json.load(open(prepare_path, "r", encoding="utf-8")).get("assets", assets)
except Exception:
    pass

payload = {
    "status": "failure",
    "skip_reason": os.environ.get("BENCH_MGPU_SKIP_REASON", "unknown"),
    "exit_code": 1,
    "stage": "multi_gpu",
    "task": "train",
    "command": os.environ.get("BENCH_MGPU_COMMAND", ""),
    "timeout_sec": int(os.environ.get("BENCH_MGPU_TIMEOUT_SEC", "1200")),
    "framework": "pytorch",
    "assets": assets,
    "meta": {
        "python": os.environ.get("BENCH_MGPU_PYTHON_EXE", ""),
        "python_source": os.environ.get("BENCH_MGPU_PYTHON_SOURCE", "unknown"),
        "git_commit": os.environ.get("BENCH_MGPU_GIT_COMMIT", ""),
        "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("BENCH_MGPU_CUDA_VISIBLE_DEVICES", "")},
        "decision_reason": "multi-gpu stage precheck failed",
        "timestamp_utc": os.environ.get("BENCH_MGPU_TIMESTAMP_UTC", ""),
    },
    "failure_category": os.environ.get("BENCH_MGPU_FAILURE_CATEGORY", "unknown"),
    "error_excerpt": os.environ.get("BENCH_MGPU_MESSAGE", ""),
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
}

if [[ ! -s "$prepare_results" ]]; then
  write_failure_results "data" "unknown" "Missing $prepare_results; run prepare_assets.sh first." "" "" "unknown"
fi

DATASET_NAME_FOR_TRAIN="$("$BOOTSTRAP_PY" -c 'import json,sys; d=json.load(open(sys.argv[1],"r",encoding="utf-8")); print(d.get("meta",{}).get("dataset",{}).get("train_dataset_name_arg",""))' "$prepare_results" 2>/dev/null || true)"
if [[ -z "$DATASET_NAME_FOR_TRAIN" ]]; then
  write_failure_results "data" "unknown" "Missing meta.dataset.train_dataset_name_arg in $prepare_results" "" "" "unknown"
fi

PYTHON_EXE=""
python_source="unknown"
if [[ -n "$python_bin" ]]; then
  PYTHON_EXE="$python_bin"
  python_source="cli"
else
  rp_args=()
  [[ -n "$report_path" ]] && rp_args+=(--report-path "$report_path")
  resolved="$("$BOOTSTRAP_PY" benchmark_scripts/runner.py resolve-python --require-report "${rp_args[@]}" || true)"
  PYTHON_EXE="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("python",""))' <<<"$resolved" 2>/dev/null || true)"
  err="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("error",""))' <<<"$resolved" 2>/dev/null || true)"
  if [[ -z "$PYTHON_EXE" || -n "$err" ]]; then
    write_failure_results "missing_report" "unknown" "python resolution failed: ${err:-missing_report}" "" "" "report"
  fi
  python_source="report"
fi

if ! "$PYTHON_EXE" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  write_failure_results "path_hallucination" "unknown" "Resolved python is not executable: $PYTHON_EXE" "" "$PYTHON_EXE" "$python_source"
fi

set +e
gpu_probe_out="$(CUDA_VISIBLE_DEVICES="$cuda_visible_devices" "$PYTHON_EXE" -c 'import torch; print(torch.cuda.device_count())' 2>&1)"
gpu_probe_rc=$?
set -e
if [[ $gpu_probe_rc -ne 0 ]]; then
  write_failure_results "deps" "unknown" "Failed to import torch or query GPUs (rc=$gpu_probe_rc): $gpu_probe_out" "" "$PYTHON_EXE" "$python_source"
fi

gpu_count="$(printf "%s\n" "$gpu_probe_out" | tail -n 1 | tr -d '\r' | tr -d ' ')"
if [[ ! "$gpu_count" =~ ^[0-9]+$ ]]; then
  gpu_count="0"
fi

intended_cmd="CUDA_VISIBLE_DEVICES=$cuda_visible_devices $PYTHON_EXE -m accelerate.commands.launch --num_processes $num_processes --mixed_precision no --dynamo_backend no src/f5_tts/train/train.py --config-name F5TTS_Small.yaml ..."

if [[ "$gpu_count" -lt 2 ]]; then
  write_failure_results "runtime" "insufficient_hardware" "Need >=2 GPUs, got torch.cuda.device_count()=$gpu_count with CUDA_VISIBLE_DEVICES=$cuda_visible_devices" "$intended_cmd" "$PYTHON_EXE" "$python_source"
fi

runner_args=(run --stage multi_gpu --task train --framework pytorch --timeout-sec "$timeout_sec" --decision-reason "Use accelerate launch + src/f5_tts/train/train.py (Hydra) for distributed DDP training; override config to 1 epoch, batch_size=1, minimal dataset from prepare_assets.sh, and outputs under build_output/multi_gpu.")
[[ -n "$python_bin" ]] && runner_args+=(--python "$python_bin")
[[ -n "$report_path" ]] && runner_args+=(--report-path "$report_path")

set +e
"$BOOTSTRAP_PY" benchmark_scripts/runner.py "${runner_args[@]}" \
  --env "CUDA_VISIBLE_DEVICES=$cuda_visible_devices" \
  --py-module accelerate.commands.launch --py-args \
    --num_processes "$num_processes" \
    --mixed_precision no \
    --dynamo_backend no \
    src/f5_tts/train/train.py \
      --config-name F5TTS_Small.yaml \
      hydra.run.dir=build_output/multi_gpu/hydra_run \
      ckpts.save_dir=build_output/multi_gpu/ckpts \
      ckpts.logger=null \
      ckpts.log_samples=False \
      ckpts.save_per_updates=999999 \
      ckpts.keep_last_n_checkpoints=0 \
      ckpts.last_per_updates=999999 \
      datasets.name="$DATASET_NAME_FOR_TRAIN" \
      datasets.batch_size_type=sample \
      datasets.batch_size_per_gpu=1 \
      datasets.max_samples=1 \
      datasets.num_workers=1 \
      optim.epochs=1 \
      optim.num_warmup_updates=0 \
      optim.grad_accumulation_steps=1
rc=$?
set -e
exit "$rc"
