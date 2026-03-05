#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal CPU inference via repository entrypoint (infer.py).

Outputs:
  build_output/cpu/log.txt
  build_output/cpu/results.json

Optional:
  --report-path <path>   Passed through to runner.py (python resolution)
  --python <path>        Passed through to runner.py (highest priority)
EOF
}

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
stage="cpu"
prepare_results="$repo_root/build_output/prepare/results.json"
report_path=""
python_bin=""

while [[ $# -gt 0 ]]; do
  case "$1" in
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

dataset_path="$(python3 - <<PY 2>/dev/null || true
import json, pathlib
p=pathlib.Path(${prepare_results@Q})
try:
  d=json.loads(p.read_text(encoding="utf-8"))
  print(d.get("assets",{}).get("dataset",{}).get("path",""))
except Exception:
  print("")
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

if [[ -z "$dataset_path" || -z "$model_path" ]]; then
  out_dir="$repo_root/build_output/$stage"
  mkdir -p "$out_dir"
  log_path="$out_dir/log.txt"
  : >"$log_path"
  echo "[cpu] missing prepare assets; expected: $prepare_results" | tee -a "$log_path"
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
  "stage":"cpu",
  "task":"infer",
  "command":"",
  "timeout_sec":600,
  "framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"prepare stage results missing or invalid"},
  "failure_category":"data",
  "error_excerpt":tail(),
}
(out_dir/"results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
PY
  exit 1
fi

runner_args=(python3 benchmark_scripts/runner.py --stage "$stage" --task infer --framework pytorch --assets-from "$prepare_results" --decision-reason "Use infer.py (repo entrypoint) on a single example scene; force CPU by hiding CUDA; resolution=64; precision=fp32." --env "CUDA_VISIBLE_DEVICES=" --env "ATTN_IMPL=sdpa" --env "HF_HOME=$repo_root/benchmark_assets/cache/huggingface" --env "HF_HUB_CACHE=$repo_root/benchmark_assets/cache/huggingface/hub" --env "TRANSFORMERS_CACHE=$repo_root/benchmark_assets/cache/huggingface/transformers" --env "TORCH_HOME=$repo_root/benchmark_assets/cache/torch" --env "XDG_CACHE_HOME=$repo_root/benchmark_assets/cache/xdg" --env "IMAGEIO_USERDIR=$repo_root/benchmark_assets/cache/imageio" --env "TMPDIR=$repo_root/benchmark_assets/cache/tmp")
[[ -n "$report_path" ]] && runner_args+=(--report-path "$report_path")
[[ -n "$python_bin" ]] && runner_args+=(--python "$python_bin")

"${runner_args[@]}" -- {python} infer.py --h5_file "$dataset_path" --model_id "$model_path" --precision fp32 --resolution 64 --output_dir "build_output/$stage/output"

