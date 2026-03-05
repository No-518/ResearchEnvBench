#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ROOT_DIR/build_output/prepare"
mkdir -p "$OUT_DIR"
LOG_PATH="$OUT_DIR/log.txt"
RESULTS_PATH="$OUT_DIR/results.json"

HOST_PYTHON="$(command -v python3 || command -v python || true)"
if [[ -z "$HOST_PYTHON" ]]; then
  {
    echo "[prepare] No python/python3 found in PATH."
    echo "[prepare] Cannot run asset preparation."
  } >"$LOG_PATH"
  cat >"$RESULTS_PATH" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "prepare_assets.sh",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "host python missing"},
  "failure_category": "deps",
  "error_excerpt": "No python/python3 found in PATH."
}
JSON
  exit 1
fi

export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

# Prefer the bench python from report.json for downloads (HF hub, etc.).
RUN_PYTHON="$HOST_PYTHON"
set +e
RESOLVED_PYTHON="$("$HOST_PYTHON" "$ROOT_DIR/benchmark_scripts/runner.py" resolve-python --allow-fallback 2>/dev/null)"
set -e
if [[ -n "${RESOLVED_PYTHON:-}" ]]; then
  RUN_PYTHON="$RESOLVED_PYTHON"
fi

set +e
"$RUN_PYTHON" "$ROOT_DIR/benchmark_scripts/prepare_assets_impl.py" "$@" >"$LOG_PATH" 2>&1
RC=$?
set -e

if [[ ! -f "$RESULTS_PATH" ]]; then
  echo "[prepare] ERROR: results.json was not written by prepare_assets_impl.py" >>"$LOG_PATH"
  cat >"$RESULTS_PATH" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "$HOST_PYTHON benchmark_scripts/prepare_assets_impl.py",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {"python": "$HOST_PYTHON", "git_commit": "", "env_vars": {}, "decision_reason": "impl did not write results.json"},
  "failure_category": "unknown",
  "error_excerpt": "prepare_assets_impl.py did not write build_output/prepare/results.json"
}
JSON
  exit 1
fi

exit "$RC"
