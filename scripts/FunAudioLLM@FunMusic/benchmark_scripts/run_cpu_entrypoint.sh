#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage="cpu"
out_dir="$repo_root/build_output/$stage"
mkdir -p "$out_dir"

prepare_results="$repo_root/build_output/prepare/results.json"

echo "[cpu] SKIPPED: CPU stage is intentionally disabled (strict benchmark configuration)." >"$out_dir/log.txt"

python - <<'PY' "$out_dir/results.json" "$prepare_results" "$repo_root"
import json, sys, subprocess
from pathlib import Path

out_path = Path(sys.argv[1])
prepare_path = Path(sys.argv[2])
repo_root = Path(sys.argv[3])

assets = {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}}
meta_extra = {}
if prepare_path.exists():
    try:
        d = json.loads(prepare_path.read_text(encoding="utf-8"))
        if isinstance(d.get("assets"), dict):
            assets = d["assets"]
        if isinstance(d.get("meta"), dict):
            meta_extra = {k: d["meta"].get(k) for k in ("dataset_list_1row", "benchmark_config_path") if k in d["meta"]}
    except Exception:
        pass

git_commit = ""
try:
    git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True).strip()
except Exception:
    git_commit = ""

payload = {
  "status": "skipped",
  "skip_reason": "not_applicable",
  "exit_code": 0,
  "stage": "cpu",
  "task": "infer",
  "command": "bash benchmark_scripts/run_cpu_entrypoint.sh",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": assets,
  "meta": {
    "python": "",
    "python_version": "",
    "git_commit": git_commit,
    "env_vars": {},
    "decision_reason": "CPU stage intentionally skipped (strict benchmark configuration).",
    **meta_extra,
  },
  "failure_category": "not_applicable",
  "error_excerpt": "",
}

out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY

exit 0
