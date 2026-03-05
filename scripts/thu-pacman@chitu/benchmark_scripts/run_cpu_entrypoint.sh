#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 1

out_dir="$repo_root/build_output/cpu"
mkdir -p "$out_dir"
log_file="$out_dir/log.txt"
results_json="$out_dir/results.json"

exec > >(tee "$log_file") 2>&1

assets_json='{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}}'
if [[ -f "$repo_root/build_output/prepare/results.json" ]]; then
  assets_json="$(jq -c '.assets // empty' "$repo_root/build_output/prepare/results.json" 2>/dev/null || echo "$assets_json")"
  [[ -z "$assets_json" || "$assets_json" == "null" ]] && assets_json='{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}}'
fi

# Reliable evidence in repo docs: docs/zh/FAQ.md explicitly says pure-CPU inference is not supported yet.
evidence_file="$repo_root/docs/zh/FAQ.md"
skip_reason="repo_not_supported"
decision_reason="Chitu docs state pure-CPU inference is planned (not supported). See docs/zh/FAQ.md Q7."

if [[ -f "$evidence_file" ]]; then
  if command -v rg >/dev/null 2>&1; then
    echo "[cpu] Skipping CPU stage: $(rg -n \"纯CPU推理\" \"$evidence_file\" | head -n 1 || true)"
  else
    echo "[cpu] Skipping CPU stage: $(grep -n \"纯CPU推理\" \"$evidence_file\" | head -n 1 || true)"
  fi
else
  echo "[cpu] Skipping CPU stage: docs evidence file not found; defaulting to skip based on repository entrypoints requiring CUDA."
fi

cat >"$results_json" <<JSON
{
  "status": "skipped",
  "skip_reason": "$skip_reason",
  "exit_code": 0,
  "stage": "cpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": $assets_json,
  "meta": {
    "python": "",
    "git_commit": "$(git rev-parse HEAD 2>/dev/null || true)",
    "env_vars": {
      "CUDA_VISIBLE_DEVICES": ""
    },
    "decision_reason": "$decision_reason"
  },
  "failure_category": "not_applicable",
  "error_excerpt": ""
}
JSON
exit 0
