#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_DIR="$REPO_ROOT/build_output/cpu"
mkdir -p "$OUT_DIR"

LOG_FILE="$OUT_DIR/log.txt"
RESULTS_JSON="$OUT_DIR/results.json"

GIT_COMMIT=""
if command -v git >/dev/null 2>&1; then
  GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
fi

{
  echo "[cpu] repo_root=$REPO_ROOT"
  echo "[cpu] started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[cpu] decision: skipped (repo_not_supported)"
  echo "[cpu] evidence: official entrypoint scripts (e.g. benchmark/benchmark_longctx.py) hardcode dist.init_process_group(\"nccl\") and torch.device(\"cuda:<rank>\") with no CPU flags."
  echo "[cpu] ended_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"$LOG_FILE"

python3 - <<PY >"$RESULTS_JSON"
import json, time, pathlib
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
repo = pathlib.Path(${REPO_ROOT@Q}).resolve()
payload = {
  "status": "skipped",
  "skip_reason": "repo_not_supported",
  "exit_code": 0,
  "stage": "cpu",
  "task": "infer",
  "command": "SKIPPED: repo entrypoints require CUDA/NCCL (no CPU mode exposed)",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": str((repo / "benchmark_assets" / "dataset").resolve()), "source": "not_applicable", "version": "unknown", "sha256": ""},
    "model":   {"path": str((repo / "benchmark_assets" / "model").resolve()),   "source": "not_applicable", "version": "unknown", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": ${GIT_COMMIT@Q},
    "env_vars": {k: "" for k in ["CUDA_VISIBLE_DEVICES"]},
    "decision_reason": "CPU execution is not exposed by the native benchmark entrypoints: they initialize NCCL and select CUDA devices unconditionally (e.g., benchmark/benchmark_longctx.py).",
    "timestamp_utc": utc(),
  },
  "failure_category": "cpu_not_supported",
  "error_excerpt": "",
}
print(json.dumps(payload, indent=2))
PY

exit 0

