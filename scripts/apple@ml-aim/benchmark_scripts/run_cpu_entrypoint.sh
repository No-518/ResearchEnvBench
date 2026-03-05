#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_DIR="$ROOT/build_output/cpu"
LOG_TXT="$STAGE_DIR/log.txt"
RESULTS_JSON="$STAGE_DIR/results.json"
PREP_RESULTS="$ROOT/build_output/prepare/results.json"
MANIFEST_ENV="$ROOT/benchmark_assets/manifest.env"

mkdir -p "$STAGE_DIR"

git_commit="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"

dataset_path=""
dataset_source=""
dataset_version=""
dataset_sha256=""
model_path=""
model_source=""
model_version=""
model_sha256=""

if [[ -f "$PREP_RESULTS" ]]; then
  python3 - "$PREP_RESULTS" <<'PY' >"$STAGE_DIR/.assets.env" 2>/dev/null || true
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
data = json.loads(p.read_text(encoding="utf-8"))
assets = data.get("assets", {})
ds = assets.get("dataset", {}) if isinstance(assets, dict) else {}
md = assets.get("model", {}) if isinstance(assets, dict) else {}
def s(x): return "" if x is None else str(x)
print(f"DATASET_PATH={s(ds.get('path'))!r}")
print(f"DATASET_SOURCE={s(ds.get('source'))!r}")
print(f"DATASET_VERSION={s(ds.get('version'))!r}")
print(f"DATASET_SHA256={s(ds.get('sha256'))!r}")
print(f"MODEL_PATH={s(md.get('path'))!r}")
print(f"MODEL_SOURCE={s(md.get('source'))!r}")
print(f"MODEL_VERSION={s(md.get('version'))!r}")
print(f"MODEL_SHA256={s(md.get('sha256'))!r}")
PY
  # shellcheck disable=SC1090
  source "$STAGE_DIR/.assets.env" || true
  dataset_path="${DATASET_PATH:-}"
  dataset_source="${DATASET_SOURCE:-}"
  dataset_version="${DATASET_VERSION:-}"
  dataset_sha256="${DATASET_SHA256:-}"
  model_path="${MODEL_PATH:-}"
  model_source="${MODEL_SOURCE:-}"
  model_version="${MODEL_VERSION:-}"
  model_sha256="${MODEL_SHA256:-}"
fi

{
  echo "[cpu] repo_root=$ROOT"
  echo "[cpu] entrypoint=aim-v1/main_attnprobe.py"
  echo "[cpu] decision: skip (repo entrypoint hardcodes CUDA)"
  echo ""
  echo "[cpu] evidence: aim-v1/main_attnprobe.py contains .cuda() calls:"
  rg -n "\\.cuda\\(" "$ROOT/aim-v1/main_attnprobe.py" || true
  echo ""
  echo "[cpu] evidence: aim-v1/utils.init_distributed_mode() raises when no CUDA:"
  rg -n "Please ensure that at least 1 GPU is available" "$ROOT/aim-v1/aim/v1/utils.py" || true
} >"$LOG_TXT"

python3 - "$RESULTS_JSON" <<PY
import json
from pathlib import Path

out = Path("$RESULTS_JSON")
payload = {
  "status": "skipped",
  "skip_reason": "repo_not_supported",
  "exit_code": 0,
  "stage": "cpu",
  "task": "infer",
  "command": "CUDA_VISIBLE_DEVICES='' python -m torch.distributed.run ... aim-v1/main_attnprobe.py (not runnable on CPU; skipped)",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "$dataset_path", "source": "$dataset_source", "version": "$dataset_version", "sha256": "$dataset_sha256"},
    "model": {"path": "$model_path", "source": "$model_source", "version": "$model_version", "sha256": "$model_sha256"},
  },
  "meta": {
    "python": "",
    "git_commit": "$git_commit",
    "env_vars": {"CUDA_VISIBLE_DEVICES": ""},
    "decision_reason": "The only repo entrypoint (aim-v1/main_attnprobe.py) unconditionally uses CUDA (model.cuda(), inp.cuda()) and distributed init requires at least 1 GPU; no CPU flags are exposed. Stage skipped per repo_not_supported.",
  },
  "failure_category": "cpu_not_supported",
  "error_excerpt": "",
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY

exit 0

