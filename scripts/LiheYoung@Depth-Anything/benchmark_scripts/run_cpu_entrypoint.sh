#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step CPU inference via the repository entrypoint.

Entrypoint (from README):
  python run.py --encoder <vits|vitb|vitl> --img-path <dir|file|txt> --outdir <outdir>

Outputs (always written, even on failure):
  build_output/cpu/log.txt
  build_output/cpu/results.json

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

stage_dir="$repo_root/build_output/cpu"
mkdir -p "$stage_dir"
log_file="$stage_dir/log.txt"
results_json="$stage_dir/results.json"

sys_py="$(command -v python3 || command -v python || true)"
if [[ -z "$sys_py" ]]; then
  printf '%s\n' "python/python3 not found on PATH" >"$log_file"
  # Best-effort write minimal results.json
  cat >"$results_json" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "cpu",
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
  "status":"failure","skip_reason":"not_applicable","exit_code":1,"stage":"cpu","task":"infer",
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
  "status":"failure","skip_reason":"not_applicable","exit_code":1,"stage":"cpu","task":"infer",
  "command":"","timeout_sec":int(${timeout_sec@Q}),"framework":"pytorch",
  "assets":{"dataset":{"path":${dataset_path@Q},"source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"dataset_path missing/invalid"},
  "failure_category":"data","error_excerpt":"dataset_path missing/invalid"
}
print(json.dumps(payload, indent=2))
PY
  exit 1
fi

outdir="$repo_root/build_output/cpu/out"
mkdir -p "$outdir"

export HF_HOME="$repo_root/benchmark_assets/cache/huggingface"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HUB_DISABLE_TELEMETRY=1

decision_reason="CPU run via README entrypoint run.py; forced CPU by CUDA_VISIBLE_DEVICES=\"\"; 1-step ensured by dataset containing 1 image."

"$sys_py" benchmark_scripts/runner.py \
  --stage cpu \
  --task infer \
  --timeout-sec "$timeout_sec" \
  --framework pytorch \
  --assets-from "$prepare_results" \
  --decision-reason "$decision_reason" \
  --env "CUDA_VISIBLE_DEVICES=" \
  --env "HF_HOME=$HF_HOME" \
  --env "HUGGINGFACE_HUB_CACHE=$HUGGINGFACE_HUB_CACHE" \
  --env "TORCH_HOME=$TORCH_HOME" \
  --env "XDG_CACHE_HOME=$XDG_CACHE_HOME" \
  -- python run.py --encoder "$encoder" --img-path "$dataset_path" --outdir "$outdir" --pred-only --grayscale

