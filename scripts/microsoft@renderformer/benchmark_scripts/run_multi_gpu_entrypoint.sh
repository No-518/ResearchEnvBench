#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU inference via repository entrypoint (infer.py) using torchrun.

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Optional:
  --visible-devices <csv>   Default: 0,1 (overrides torch visibility for this stage)
  --report-path <path>      Passed through to runner.py (python resolution)
  --python <path>           Passed through to runner.py (highest priority)
EOF
}

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
stage="multi_gpu"
prepare_results="$repo_root/build_output/prepare/results.json"
report_path=""
python_bin=""
visible_devices="${SCIMLOPSBENCH_MULTI_GPU_VISIBLE_DEVICES:-0,1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --visible-devices)
      visible_devices="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --python)
      python_bin="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

cd "$repo_root"

dataset_files_json="$(python3 - <<PY 2>/dev/null || true
import json, pathlib
p=pathlib.Path(${prepare_results@Q})
try:
  d=json.loads(p.read_text(encoding="utf-8"))
  print(json.dumps(d.get("meta",{}).get("dataset_files",[])))
except Exception:
  print("[]")
PY
)"

dataset_files_count="$(python3 - <<PY 2>/dev/null || true
import json
try:
  arr=json.loads(${dataset_files_json@Q})
  print(len(arr))
except Exception:
  print(0)
PY
)"

if [[ "${dataset_files_count:-0}" -lt 2 ]]; then
  dataset_files_json="$(python3 - <<PY 2>/dev/null || true
import json, pathlib
root = pathlib.Path(${repo_root@Q}) / "benchmark_assets" / "dataset"
paths = sorted(str(p) for p in root.rglob("*.h5")) if root.exists() else []
print(json.dumps(paths))
PY
  )"
fi

dataset_path_0="$(python3 - <<PY 2>/dev/null || true
import json
arr=json.loads(${dataset_files_json@Q})
print(arr[0] if len(arr)>0 else "")
PY
)"
dataset_path_1="$(python3 - <<PY 2>/dev/null || true
import json
arr=json.loads(${dataset_files_json@Q})
print(arr[1] if len(arr)>1 else "")
PY
)"
model_path="$(python3 - <<PY 2>/dev/null || true
import json, pathlib
p=pathlib.Path(${prepare_results@Q})
try:
  d=json.loads(p.read_text(encoding="utf-8"))
  print(d.get("assets",{}).get("model",{}).get("path",""))
except Exception:
  print("")
PY
)"

if [[ -z "$dataset_path_0" || -z "$dataset_path_1" || -z "$model_path" ]]; then
  out_dir="$repo_root/build_output/$stage"
  mkdir -p "$out_dir"
  log_path="$out_dir/log.txt"
  : >"$log_path"
  echo "[$stage] missing prepare assets; expected: $prepare_results (need 2 dataset files)" | tee -a "$log_path"
  python3 - <<PY
import json, pathlib
out_dir=pathlib.Path(${out_dir@Q})
log_path=out_dir/"log.txt"
def tail(n=220):
  try:
    lines=log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])
  except Exception:
    return ""
payload={
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"multi_gpu",
  "task":"infer",
  "command":"",
  "timeout_sec":1200,
  "framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"prepare stage results missing/invalid or did not provide 2 dataset files"},
  "failure_category":"data",
  "error_excerpt":tail(),
}
(out_dir/"results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
PY
  exit 1
fi

runner_args=(python3 benchmark_scripts/runner.py --stage "$stage" --task infer --framework pytorch --assets-from "$prepare_results" --timeout-sec 1200 --decision-reason "Use torchrun to run infer.py twice (two ranks) on two GPUs; map LOCAL_RANK to visible GPU IDs; one scene per rank; resolution=64; precision=fp16." --env "CUDA_VISIBLE_DEVICES=$visible_devices" --env "ATTN_IMPL=sdpa" --env "HF_HOME=$repo_root/benchmark_assets/cache/huggingface" --env "HF_HUB_CACHE=$repo_root/benchmark_assets/cache/huggingface/hub" --env "TRANSFORMERS_CACHE=$repo_root/benchmark_assets/cache/huggingface/transformers" --env "TORCH_HOME=$repo_root/benchmark_assets/cache/torch" --env "XDG_CACHE_HOME=$repo_root/benchmark_assets/cache/xdg" --env "IMAGEIO_USERDIR=$repo_root/benchmark_assets/cache/imageio" --env "TMPDIR=$repo_root/benchmark_assets/cache/tmp")
[[ -n "$report_path" ]] && runner_args+=(--report-path "$report_path")
[[ -n "$python_bin" ]] && runner_args+=(--python "$python_bin")

"${runner_args[@]}" -- bash -lc 'set -euo pipefail; PY="$1"; MODEL="$2"; H5_0="$3"; H5_1="$4"; OUT_BASE="$5"; VISIBLE="$6"; export CUDA_VISIBLE_DEVICES="$VISIBLE"; "$PY" -c "import torch, sys; print(\"cuda_available=%s device_count=%s\" % (torch.cuda.is_available(), torch.cuda.device_count())); sys.exit(0 if (torch.cuda.is_available() and torch.cuda.device_count()>=2) else 1)"; torchrun --standalone --nproc_per_node=2 bash -lc '"'"'set -euo pipefail; PY="$1"; MODEL="$2"; H5_0="$3"; H5_1="$4"; OUT_BASE="$5"; VISIBLE="$6"; IFS="," read -r -a DEV_ARR <<< "$VISIBLE"; GPU="${DEV_ARR[$LOCAL_RANK]:-}"; if [[ -z "$GPU" ]]; then echo "rank $LOCAL_RANK has no GPU mapping for VISIBLE=$VISIBLE" >&2; exit 1; fi; if [[ "$LOCAL_RANK" == "0" ]]; then H5="$H5_0"; else H5="$H5_1"; fi; OUTDIR="${OUT_BASE}/output_rank${LOCAL_RANK}"; mkdir -p "$OUTDIR"; ATTN_IMPL=sdpa CUDA_VISIBLE_DEVICES="$GPU" "$PY" infer.py --h5_file "$H5" --model_id "$MODEL" --precision fp16 --resolution 64 --output_dir "$OUTDIR"'"'"' bash "$PY" "$MODEL" "$H5_0" "$H5_1" "$OUT_BASE" "$VISIBLE"' bash {python} "$model_path" "$dataset_path_0" "$dataset_path_1" "build_output/$stage" "$visible_devices"
