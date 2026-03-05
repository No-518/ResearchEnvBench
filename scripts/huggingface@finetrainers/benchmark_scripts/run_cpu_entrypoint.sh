#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
CPU stage runner (FORCE-SKIPPED).

This repository's CPU mode is not benchmarked in this harness run. The stage always:
  - writes build_output/cpu/{log.txt,results.json}
  - sets status="skipped", exit_code=0

Options:
  --out-dir <path>       Root output dir (default: build_output)
  --timeout-sec <n>      Included in results.json (default: 600)
  --report-path <path>   Accepted for CLI compatibility (ignored)
  --python <path>        Accepted for CLI compatibility (ignored)
EOF
}

report_path=""
python_override=""
out_root="build_output"
timeout_sec="600"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --python)
      python_override="${2:-}"; shift 2 ;;
    --out-dir)
      out_root="${2:-}"; shift 2 ;;
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
out_root_abs="$(cd "$repo_root" && mkdir -p "$out_root" && cd "$out_root" && pwd)"
stage_dir="$out_root_abs/cpu"
mkdir -p "$stage_dir"

sys_python="$(command -v python3 || command -v python || true)"
if [[ -z "$sys_python" ]]; then
  cat >"$stage_dir/log.txt" <<EOF
[cpu] skipped: not_applicable
[cpu] reason: cpu stage force-skipped by benchmark policy
EOF
  cat >"$stage_dir/results.json" <<JSON
{
  "status": "skipped",
  "skip_reason": "not_applicable",
  "exit_code": 0,
  "stage": "cpu",
  "task": "train",
  "command": "skipped_by_benchmark_policy",
  "timeout_sec": ${timeout_sec},
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "CPU stage force-skipped by benchmark policy override."
  },
  "failure_category": "",
  "error_excerpt": ""
}
JSON
  exit 0
fi

decision_reason="CPU stage force-skipped by benchmark policy override."

"$sys_python" benchmark_scripts/runner.py \
  --stage cpu \
  --task train \
  --framework pytorch \
  --out-root "$out_root_abs" \
  --timeout-sec "$timeout_sec" \
  --decision-reason "$decision_reason" \
  --no-requires-python \
  --skip \
  --skip-reason not_applicable \
  -- \
  skipped_by_benchmark_policy

exit 0

