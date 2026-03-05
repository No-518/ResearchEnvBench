#!/usr/bin/env bash
set -uo pipefail

stage="cpu"
task="infer"
framework="pytorch"
timeout_sec=600

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/$stage"
prepare_results="$repo_root/build_output/prepare/results.json"

mkdir -p "$stage_dir"

fail() {
  local category="$1"
  local msg="$2"
  mkdir -p "$stage_dir"
  printf '%s\n' "$msg" >"$stage_dir/log.txt"
  STAGE_DIR="$stage_dir" STATUS="failure" EXIT_CODE="1" FAILURE_CATEGORY="$category" COMMAND="" DECISION_REASON="$msg" \
    python - <<'PY'
import json, os, pathlib
stage_dir = pathlib.Path(os.environ["STAGE_DIR"])
results_json = stage_dir / "results.json"
log_path = stage_dir / "log.txt"
def tail(p: pathlib.Path, n: int = 220) -> str:
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-n:])
payload = {
  "status": os.environ.get("STATUS", "failure"),
  "skip_reason": "unknown",
  "exit_code": int(os.environ.get("EXIT_CODE", "1")),
  "stage": "cpu",
  "task": "infer",
  "command": os.environ.get("COMMAND", ""),
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "")},
    "decision_reason": os.environ.get("DECISION_REASON", ""),
  },
  "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
  "error_excerpt": tail(log_path),
}
results_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
}

if [[ ! -f "$prepare_results" ]]; then
  fail "data" "Missing prepare stage results: $prepare_results"
fi

dataset_path="$(python - <<PY 2>/dev/null || true
import json, pathlib
p = pathlib.Path(${prepare_results@Q})
data = json.loads(p.read_text(encoding="utf-8"))
print(data.get("assets", {}).get("dataset", {}).get("path", ""))
PY
)"
model_path="$(python - <<PY 2>/dev/null || true
import json, pathlib
p = pathlib.Path(${prepare_results@Q})
data = json.loads(p.read_text(encoding="utf-8"))
print(data.get("assets", {}).get("model", {}).get("path", ""))
PY
)"

if [[ -z "$dataset_path" || ! -d "$dataset_path" ]]; then
  fail "data" "Prepared dataset path not found: $dataset_path"
fi
if [[ -z "$model_path" || ! -f "$model_path" ]]; then
  fail "model" "Prepared model file not found: $model_path"
fi

out_images_dir="$stage_dir/outputs"
mkdir -p "$out_images_dir"

decision_reason="Inference via official metric entrypoint metric_depth/run.py with 1 image; force CPU via CUDA_VISIBLE_DEVICES=''."

CUDA_VISIBLE_DEVICES="" \
  python "$repo_root/benchmark_scripts/runner.py" \
    --stage "$stage" \
    --task "$task" \
    --framework "$framework" \
    --out-dir "$stage_dir" \
    --timeout-sec "$timeout_sec" \
    --assets-from "$prepare_results" \
    --decision-reason "$decision_reason" \
    --python-script "metric_depth/run.py" \
    -- \
      --encoder vits \
      --load-from "$model_path" \
      --max-depth 20 \
      --img-path "$dataset_path" \
      --outdir "$out_images_dir" \
      --input-size 224 \
      --pred-only \
      --grayscale

exit $?
