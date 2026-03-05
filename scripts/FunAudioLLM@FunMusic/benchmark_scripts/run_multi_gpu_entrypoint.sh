#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage="multi_gpu"
out_dir="$repo_root/build_output/$stage"
mkdir -p "$out_dir"

prepare_results="$repo_root/build_output/prepare/results.json"

gpus="${MULTI_GPU_CUDA_VISIBLE_DEVICES:-0,1}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gpus)
      gpus="${2:-}"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--gpus 0,1]"; exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$prepare_results" ]]; then
  err="Missing prepare results at $prepare_results"
  echo "$err" >"$out_dir/log.txt"
  python - <<'PY' "$out_dir/results.json" "$err"
import json, sys
out=sys.argv[1]
err=sys.argv[2]
payload={
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"multi_gpu",
  "task":"train",
  "command":"bash benchmark_scripts/run_multi_gpu_entrypoint.sh",
  "timeout_sec":1200,
  "framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"requires build_output/prepare/results.json"},
  "failure_category":"missing_stage_results",
  "error_excerpt":err,
}
with open(out,"w",encoding="utf-8") as f:
  json.dump(payload,f,ensure_ascii=False,indent=2)
PY
  exit 1
fi

read_vars="$(
  python - <<'PY' "$prepare_results"
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
d=json.loads(p.read_text(encoding="utf-8"))
assets=d.get("assets",{})
meta=d.get("meta",{})
dataset_list=meta.get("dataset_list_1row","")
bench_cfg=meta.get("benchmark_config_path","")
model_path=(assets.get("model",{}) or {}).get("path","")
print(dataset_list)
print(bench_cfg)
print(model_path)
PY
)"

dataset_list="$(echo "$read_vars" | sed -n '1p')"
bench_cfg="$(echo "$read_vars" | sed -n '2p')"
model_path="$(echo "$read_vars" | sed -n '3p')"

# Resolve python from agent report (or override).
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_override="${SCIMLOPSBENCH_PYTHON:-}"
resolved_python=""
if [[ -n "$python_override" ]]; then
  resolved_python="$python_override"
elif [[ -f "$report_path" ]]; then
  resolved_python="$(
    python - <<'PY' "$report_path" 2>/dev/null || true
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
try:
  d=json.loads(p.read_text(encoding="utf-8"))
  print(d.get("python_path",""))
except Exception:
  print("")
PY
  )"
fi

if [[ -z "$resolved_python" ]]; then
  echo "[multi_gpu] ERROR: unable to resolve python (missing $report_path and no SCIMLOPSBENCH_PYTHON)."
  python - <<'PY' "$out_dir/results.json" "$report_path"
import json, sys
out=sys.argv[1]
report_path=sys.argv[2]
payload={
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"multi_gpu",
  "task":"train",
  "command":"bash benchmark_scripts/run_multi_gpu_entrypoint.sh",
  "timeout_sec":1200,
  "framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"requires agent report python_path or SCIMLOPSBENCH_PYTHON","report_path":report_path},
  "failure_category":"missing_report",
  "error_excerpt":"missing report python_path",
}
with open(out,"w",encoding="utf-8") as f:
  json.dump(payload,f,ensure_ascii=False,indent=2)
PY
  exit 1
fi

nproc_per_node="$(echo "$gpus" | awk -F',' '{print NF}')"

gpu_count="$(
  CUDA_VISIBLE_DEVICES="$gpus" "$resolved_python" - <<'PY' 2>/dev/null || true
import torch
print(torch.cuda.device_count())
PY
)"

if [[ -z "$gpu_count" ]]; then
  gpu_count=0
fi

if [[ "$gpu_count" -lt 2 ]] || [[ "$nproc_per_node" -lt 2 ]]; then
  err="Need >=2 GPUs for multi-GPU run. Visible via CUDA_VISIBLE_DEVICES='$gpus': torch.cuda.device_count()=$gpu_count"
  echo "$err" >"$out_dir/log.txt"
  python - <<'PY' "$out_dir/results.json" "$err"
import json, sys
out=sys.argv[1]
err=sys.argv[2]
payload={
  "status":"failure",
  "skip_reason":"insufficient_hardware",
  "exit_code":1,
  "stage":"multi_gpu",
  "task":"train",
  "command":"bash benchmark_scripts/run_multi_gpu_entrypoint.sh",
  "timeout_sec":1200,
  "framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"Requires >=2 GPUs; failing per benchmark spec."},
  "failure_category":"runtime",
  "error_excerpt":err,
}
with open(out,"w",encoding="utf-8") as f:
  json.dump(payload,f,ensure_ascii=False,indent=2)
PY
  exit 1
fi

python "$repo_root/benchmark_scripts/runner.py" \
  --stage "$stage" \
  --task train \
  --framework pytorch \
  --timeout-sec 1200 \
  --assets-from "$prepare_results" \
  --decision-reason "Repo entrypoint inspiremusic/bin/train.py launched with torch.distributed.run (torchrun). Model=flow to keep checkpoint small; config patched in prepare for max_epoch=1/accum_grad=1; dataset is 1-row parquet list; CUDA_VISIBLE_DEVICES=$gpus." \
  --env CUDA_VISIBLE_DEVICES="$gpus" \
  --env PYTHONIOENCODING=UTF-8 \
  --env PYTHONPATH="$repo_root:$repo_root/third_party/Matcha-TTS:${PYTHONPATH:-}" \
  --env TOKENIZERS_PARALLELISM=false \
  -- \
  python -m torch.distributed.run \
    --nnodes=1 \
    --nproc_per_node="$nproc_per_node" \
    --rdzv_id=1024 \
    --rdzv_backend=c10d \
    --rdzv_endpoint=localhost:0 \
    inspiremusic/bin/train.py \
      --train_engine torch_ddp \
      --config "$bench_cfg" \
      --train_data "$dataset_list" \
      --cv_data "$dataset_list" \
      --model flow \
      --model_dir "$out_dir/model_dir" \
      --tensorboard_dir "$out_dir/tensorboard" \
      --ddp.dist_backend nccl \
      --num_workers 1 \
      --prefetch 2 \
      --pin_memory
