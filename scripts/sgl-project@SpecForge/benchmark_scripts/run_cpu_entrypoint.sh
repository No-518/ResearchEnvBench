#!/usr/bin/env bash
set -euo pipefail

# Minimal CPU run for repository entrypoint.
# SpecForge's training entrypoint (scripts/train_eagle3.py) unconditionally moves models to CUDA.
# This stage therefore records a repo-level CPU "skipped" with reviewable evidence.

usage() {
  cat <<'EOF'
Run a minimal CPU step via the repository entrypoint.

This repo's training entrypoint is CUDA-only (unconditional .cuda() / device="cuda"),
so this stage emits build_output/cpu/results.json with status="skipped".

Optional:
  --report-path <path>           Passed through to runner.py for python resolution
  --python <path>                Passed through to runner.py (highest priority)
EOF
}

python_bin=""
report_path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

host_python="$(command -v python3 || command -v python || true)"
if [[ -z "$host_python" ]]; then
  mkdir -p build_output/cpu
  cat > build_output/cpu/log.txt <<'EOF'
[cpu] ERROR: python3/python not found in PATH
EOF
  cat > build_output/cpu/results.json <<'EOF'
{"status":"failure","skip_reason":"not_applicable","exit_code":1,"stage":"cpu","task":"train","command":"","timeout_sec":600,"framework":"pytorch","assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},"meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"host python not found"},"failure_category":"deps","error_excerpt":"host python not found"}
EOF
  exit 1
fi

decision_reason="SpecForge training entrypoint scripts/train_eagle3.py hardcodes CUDA usage (e.g., .cuda() and device=\\\"cuda\\\" in build_target_model/build_draft_model), and does not expose a CPU device flag in its CLI."

runner_args=(--stage cpu --task train --framework pytorch --skip --skip-reason repo_not_supported --decision-reason "$decision_reason")
if [[ -n "$python_bin" ]]; then
  runner_args+=(--python "$python_bin")
fi
if [[ -n "$report_path" ]]; then
  runner_args+=(--report-path "$report_path")
fi

"$host_python" benchmark_scripts/runner.py "${runner_args[@]}"

