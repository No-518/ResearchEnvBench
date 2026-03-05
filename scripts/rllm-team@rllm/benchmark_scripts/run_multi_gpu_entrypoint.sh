#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal multi-GPU run via repository entrypoint (DDP).

This stage is marked as skipped (not_applicable) per benchmark configuration (user requested multi_gpu skipped).

Writes:
  build_output/multi_gpu/log.txt
  build_output/multi_gpu/results.json

Options:
  --python <path>        Explicit python executable (overrides report/env)
  --report-path <path>   Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
EOF
}

python_bin=""
report_path=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
report_path="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"
cd "$repo_root"

prepare_results="build_output/prepare/results.json"
decision_reason="Skipped by benchmark configuration (user requested multi_gpu skipped)."

runner_py="${python_bin:-python}"
set +e
"$runner_py" "$repo_root/benchmark_scripts/runner.py" run \
  --stage multi_gpu \
  --task train \
  --framework pytorch \
  --timeout-sec 1200 \
  --report-path "$report_path" \
  ${python_bin:+--python "$python_bin"} \
  --skip \
  --skip-reason "not_applicable" \
  --assets-json "$prepare_results" \
  --decision-reason "$decision_reason"
rc=$?
set -e
exit "$rc"
