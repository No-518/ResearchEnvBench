#!/usr/bin/env bash
set -u -o pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage="cpu"
stage_dir="$repo_root/build_output/$stage"
mkdir -p "$stage_dir"

prep_results="$repo_root/build_output/prepare/results.json"
assets_json="$stage_dir/assets.json"

write_failure() {
  local category="$1"
  local msg="$2"
  local log_txt="$stage_dir/log.txt"
  local results_json="$stage_dir/results.json"
  {
    echo "$msg"
  } >"$log_txt"
  MSG="$msg" CATEGORY="$category" REPO_ROOT="$repo_root" python - <<'PY' >"$results_json"
import json, os, subprocess
from pathlib import Path
repo = Path(os.environ.get("REPO_ROOT", "."))
def git_commit():
  try:
    return subprocess.check_output(["git","rev-parse","HEAD"], cwd=str(repo), text=True, timeout=5).strip()
  except Exception:
    return ""
msg = os.environ.get("MSG", "")
category = os.environ.get("CATEGORY", "unknown")
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "cpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": git_commit(),
    "env_vars": {},
    "decision_reason": msg,
  },
  "failure_category": category,
  "error_excerpt": msg,
}
print(json.dumps(payload, indent=2))
PY
  exit 1
}

if [[ ! -f "$prep_results" ]]; then
  write_failure "data" "Missing prepare results at $prep_results"
fi

model_path="$(python - <<PY 2>/dev/null || true
import json
from pathlib import Path
data = json.loads(Path(${prep_results@Q}).read_text(encoding="utf-8"))
print(data.get("assets", {}).get("model", {}).get("path", ""))
PY
)"
if [[ -z "$model_path" ]]; then
  write_failure "data" "prepare results missing assets.model.path"
fi

python - <<PY >"$assets_json"
import json
from pathlib import Path
data = json.loads(Path(${prep_results@Q}).read_text(encoding="utf-8"))
assets = data.get("assets", {}) if isinstance(data, dict) else {}
print(json.dumps(assets, indent=2))
PY

intended_cmd="{python} examples/llama.py --model $model_path --attn moba --moba-chunk-size 128 --moba-topk 2"
skip_msg=$'CPU execution is not supported by this repository entrypoint.\n\nEvidence:\n- requirements.txt pins flash-attn (CUDA extension).\n- moba/wrapper.py imports flash_attn and uses flash_attn_func in the decode phase.\n- examples/llama.py always calls model.generate(...), which triggers decode attention.\n'

python "$repo_root/benchmark_scripts/runner.py" \
  --stage cpu \
  --task infer \
  --timeout-sec 600 \
  --framework pytorch \
  --assets-json "$assets_json" \
  --skip repo_not_supported \
  --skip-command "$intended_cmd" \
  --skip-message "$skip_msg"
