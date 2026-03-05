#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_DIR="$REPO_ROOT/build_output/prepare"
ASSET_CACHE="$REPO_ROOT/benchmark_assets/cache"
DATASET_DIR="$REPO_ROOT/benchmark_assets/dataset"
MODEL_DIR="$REPO_ROOT/benchmark_assets/model"

mkdir -p "$OUT_DIR" "$ASSET_CACHE" "$DATASET_DIR" "$MODEL_DIR"

LOG_FILE="$OUT_DIR/log.txt"
RESULTS_JSON="$OUT_DIR/results.json"

GIT_COMMIT=""
if command -v git >/dev/null 2>&1; then
  GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
fi

{
  echo "[prepare] repo_root=$REPO_ROOT"
  echo "[prepare] dataset_dir=$DATASET_DIR"
  echo "[prepare] model_dir=$MODEL_DIR"
  echo "[prepare] started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[prepare] decision: skipped (not_applicable) - repo benchmarks use synthetic tensors; no dataset/model needed."
  echo "[prepare] ended_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"$LOG_FILE"

python3 - <<PY >"$RESULTS_JSON"
import json, os, time, pathlib
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
repo = pathlib.Path(${REPO_ROOT@Q}).resolve()
payload = {
  "status": "skipped",
  "skip_reason": "not_applicable",
  "exit_code": 0,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh (skipped: not_applicable)",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": str((repo / "benchmark_assets" / "dataset").resolve()), "source": "not_applicable", "version": "unknown", "sha256": ""},
    "model":   {"path": str((repo / "benchmark_assets" / "model").resolve()),   "source": "not_applicable", "version": "unknown", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": ${GIT_COMMIT@Q},
    "env_vars": {k: os.environ.get(k,"") for k in ["SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","HF_HOME","TORCH_HOME","XDG_CACHE_HOME","PIP_CACHE_DIR"]},
    "decision_reason": "Repository entrypoints generate synthetic QKV tensors and do not require an external dataset or model checkpoint; user confirmed prepare is skipped.",
    "timestamp_utc": utc(),
  },
  "failure_category": "unknown",
  "error_excerpt": "",
}
print(json.dumps(payload, indent=2))
PY

exit 0
