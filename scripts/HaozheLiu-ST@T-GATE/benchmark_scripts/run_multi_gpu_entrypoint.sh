#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Run TGATE repo entrypoint for exactly 1 step on multi-GPU (DDP/launcher).

This repository's native demo entrypoint (main.py) does not expose any distributed launch options
and hardcodes `.to("cuda")` without rank-based device selection. This stage is marked skipped as repo_not_supported.

Outputs:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Options:
  --report-path <path>   Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="$repo_root/build_output/multi_gpu"
report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
log_txt="$out_dir/log.txt"
results_json="$out_dir/results.json"
git_commit="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

mkdir -p "$out_dir"

bootstrap_py="$(command -v python3 || command -v python || true)"
if [[ -z "$bootstrap_py" ]]; then
  echo "[multi_gpu] python3/python not found in PATH" | tee "$log_txt" >&2
  cat >"$results_json" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "torchrun --nproc_per_node=2 main.py ...",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "$git_commit",
    "env_vars": {"CUDA_VISIBLE_DEVICES": "0,1"},
    "decision_reason": "python3/python not found in PATH; cannot run runner.py"
  },
  "failure_category": "deps",
  "error_excerpt": "python3/python not found in PATH"
}
JSON
  exit 1
fi

prepare_results="$repo_root/build_output/prepare/results.json"

decision_reason="Evidence: repo only provides main.py demo; no README guidance for DDP, no torch.distributed/LOCAL_RANK handling, and device is always set via .to(\"cuda\") with no rank-based device selection. Multi-GPU distributed execution is not supported by native entrypoint."

"$bootstrap_py" "$repo_root/benchmark_scripts/runner.py" \
  --stage multi_gpu --task infer --framework pytorch \
  --out-dir "$out_dir" \
  --report-path "$report_path" \
  --assets-from "$prepare_results" \
  --skip --skip-reason repo_not_supported \
  --failure-category entrypoint_not_found \
  --decision-reason "$decision_reason" \
  --command "torchrun --nproc_per_node=2 main.py ..."
