#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step single-GPU inference via the repository entrypoint.

Outputs (always written, even on failure):
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

Optional:
  --timeout-sec <n>   Default: 600
EOF
}

timeout_sec="600"
while [[ $# -gt 0 ]]; do
  case "$1" in
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

stage_dir="$repo_root/build_output/single_gpu"
mkdir -p "$stage_dir"
log_file="$stage_dir/log.txt"
results_json="$stage_dir/results.json"

sys_py="$(command -v python3 || command -v python || true)"
if [[ -z "$sys_py" ]]; then
  printf '%s\n' "python/python3 not found on PATH" >"$log_file"
  cat >"$results_json" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "single_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "python/python3 not found"},
  "failure_category": "deps",
  "error_excerpt": "python/python3 not found on PATH"
}
JSON
  exit 1
fi

prepare_results="$repo_root/build_output/prepare/results.json"
if [[ ! -f "$prepare_results" ]]; then
  printf '%s\n' "Missing prepare results: $prepare_results" >"$log_file"
  "$sys_py" - <<PY >"$results_json"
import json
payload = {
  "status":"failure","skip_reason":"not_applicable","exit_code":1,"stage":"single_gpu","task":"infer",
  "command":"","timeout_sec":int(${timeout_sec@Q}),"framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"prepare stage results missing"},
  "failure_category":"data","error_excerpt":"prepare results missing"
}
print(json.dumps(payload, indent=2))
PY
  exit 1
fi

dataset_path="$("$sys_py" - <<'PY' "$prepare_results"
import json, sys
data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
print((data.get("assets") or {}).get("dataset", {}).get("path", ""))
PY
)"

encoder="$("$sys_py" - <<'PY' "$prepare_results"
import json, re, sys
data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
src = ((data.get("assets") or {}).get("model", {}) or {}).get("source","")
m = re.search(r"depth_anything_(vit[slb])14", src)
print(m.group(1) if m else "vits")
PY
)"

if [[ -z "$dataset_path" || ! -d "$dataset_path" ]]; then
  printf '%s\n' "Invalid dataset_path from prepare results: $dataset_path" >"$log_file"
  "$sys_py" - <<PY >"$results_json"
import json
payload = {
  "status":"failure","skip_reason":"not_applicable","exit_code":1,"stage":"single_gpu","task":"infer",
  "command":"","timeout_sec":int(${timeout_sec@Q}),"framework":"pytorch",
  "assets": json.load(open(${prepare_results@Q},"r",encoding="utf-8")).get("assets", {}),
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"dataset_path missing/invalid"},
  "failure_category":"data","error_excerpt":"dataset_path missing/invalid"
}
print(json.dumps(payload, indent=2))
PY
  exit 1
fi

# Resolve the benchmark python (same as entrypoint environment).
bench_py="${SCIMLOPSBENCH_PYTHON:-}"
if [[ -z "$bench_py" ]]; then
  bench_py="$("$sys_py" benchmark_scripts/runner.py --stage single_gpu --task infer --print-python 2>>"$log_file" || true)"
fi
if [[ -z "$bench_py" ]]; then
  printf '%s\n' "Failed to resolve benchmark python; missing report?" >>"$log_file"
  "$sys_py" - <<PY >"$results_json"
import json
payload = {
  "status":"failure","skip_reason":"not_applicable","exit_code":1,"stage":"single_gpu","task":"infer",
  "command":"","timeout_sec":int(${timeout_sec@Q}),"framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"missing report/python"},
  "failure_category":"missing_report","error_excerpt":"missing report/python"
}
print(json.dumps(payload, indent=2))
PY
  exit 1
fi

# Hardware check to avoid a false "GPU success" on CPU-only systems.
gpu_ok="$("$bench_py" - <<'PY' 2>>"$log_file" || true
try:
  import torch
  print(int(torch.cuda.is_available()))
  print(int(torch.cuda.device_count()))
except Exception:
  print(0)
  print(0)
PY
)"
cuda_avail="$(printf '%s\n' "$gpu_ok" | head -n 1 || true)"
gpu_count="$(printf '%s\n' "$gpu_ok" | tail -n 1 || true)"

if [[ "${cuda_avail:-0}" != "1" || "${gpu_count:-0}" -lt 1 ]]; then
  printf '%s\n' "CUDA not available or GPU count < 1 (cuda=$cuda_avail, gpus=$gpu_count)" >>"$log_file"
  "$sys_py" - <<PY >"$results_json"
import json
payload = {
  "status":"failure","skip_reason":"insufficient_hardware","exit_code":1,"stage":"single_gpu","task":"infer",
  "command":"","timeout_sec":int(${timeout_sec@Q}),"framework":"pytorch",
  "assets": json.load(open(${prepare_results@Q},"r",encoding="utf-8")).get("assets", {}),
  "meta":{"python":${bench_py@Q},"git_commit":"","env_vars":{"CUDA_VISIBLE_DEVICES":"0"},"decision_reason":"CUDA not available / no GPU"},
  "failure_category":"insufficient_hardware","error_excerpt":"CUDA not available / no GPU"
}
print(json.dumps(payload, indent=2))
PY
  exit 1
fi

outdir="$repo_root/build_output/single_gpu/out"
mkdir -p "$outdir"

export HF_HOME="$repo_root/benchmark_assets/cache/huggingface"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HUB_DISABLE_TELEMETRY=1

decision_reason="Single-GPU run via README entrypoint run.py; forced CUDA_VISIBLE_DEVICES=0; 1-step ensured by dataset containing 1 image."

"$sys_py" benchmark_scripts/runner.py \
  --stage single_gpu \
  --task infer \
  --timeout-sec "$timeout_sec" \
  --framework pytorch \
  --assets-from "$prepare_results" \
  --decision-reason "$decision_reason" \
  --env "CUDA_VISIBLE_DEVICES=0" \
  --env "HF_HOME=$HF_HOME" \
  --env "HUGGINGFACE_HUB_CACHE=$HUGGINGFACE_HUB_CACHE" \
  --env "TORCH_HOME=$TORCH_HOME" \
  --env "XDG_CACHE_HOME=$XDG_CACHE_HOME" \
  -- python run.py --encoder "$encoder" --img-path "$dataset_path" --outdir "$outdir" --pred-only --grayscale
