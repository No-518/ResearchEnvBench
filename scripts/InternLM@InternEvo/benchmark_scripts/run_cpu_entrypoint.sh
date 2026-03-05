#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run minimal CPU entrypoint (one step) via the repository native entrypoint.

This repository is documented as GPU-targeted (doc/en/install.md requires Ampere/Hopper GPU),
so this stage is recorded as skipped with repo_not_supported.

Outputs:
  build_output/cpu/log.txt
  build_output/cpu/results.json
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

out_dir="build_output/cpu"
mkdir -p "$out_dir"
log_txt="$out_dir/log.txt"
results_json="$out_dir/results.json"
: >"$log_txt"
exec > >(tee -a "$log_txt") 2>&1

prepare_results="$REPO_ROOT/build_output/prepare/results.json"
dataset_path=""
dataset_source=""
dataset_version=""
dataset_sha256=""
model_path=""
model_source=""
model_version=""
model_sha256=""

if [[ -f "$prepare_results" ]]; then
  eval "$(python - <<PY || true
import json, pathlib
p=pathlib.Path(${prepare_results@Q})
try:
  obj=json.loads(p.read_text(encoding="utf-8"))
except Exception:
  obj={}
assets=obj.get("assets",{}) if isinstance(obj,dict) else {}
ds=assets.get("dataset",{})
md=assets.get("model",{})
def esc(s): return s.replace('\\n',' ').replace('\\r',' ')
print(f"dataset_path={esc(str(ds.get('path','')))!r}")
print(f"dataset_source={esc(str(ds.get('source','')))!r}")
print(f"dataset_version={esc(str(ds.get('version','')))!r}")
print(f"dataset_sha256={esc(str(ds.get('sha256','')))!r}")
print(f"model_path={esc(str(md.get('path','')))!r}")
print(f"model_source={esc(str(md.get('source','')))!r}")
print(f"model_version={esc(str(md.get('version','')))!r}")
print(f"model_sha256={esc(str(md.get('sha256','')))!r}")
PY
)"
fi

decision_reason="InternEvo does not implement a CPU accelerator path: internlm/accelerator/abstract_accelerator.py only selects {cuda,npu,dipu,ditorch} and defaults to cuda, and internlm/accelerator/cuda_accelerator.py hard-codes the distributed backend to NCCL. Together with doc/en/install.md (GPU + CUDA>=11.8), CPU-only execution is treated as not supported by design for this benchmark."

git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
error_excerpt="$(tail -n 200 "$log_txt" || true)"

export SCIMLOPSBENCH_CPU_DATASET_PATH="$dataset_path"
export SCIMLOPSBENCH_CPU_DATASET_SOURCE="$dataset_source"
export SCIMLOPSBENCH_CPU_DATASET_VERSION="$dataset_version"
export SCIMLOPSBENCH_CPU_DATASET_SHA256="$dataset_sha256"
export SCIMLOPSBENCH_CPU_MODEL_PATH="$model_path"
export SCIMLOPSBENCH_CPU_MODEL_SOURCE="$model_source"
export SCIMLOPSBENCH_CPU_MODEL_VERSION="$model_version"
export SCIMLOPSBENCH_CPU_MODEL_SHA256="$model_sha256"
export SCIMLOPSBENCH_CPU_DECISION_REASON="$decision_reason"
export SCIMLOPSBENCH_CPU_GIT_COMMIT="$git_commit"
export SCIMLOPSBENCH_CPU_ERROR_EXCERPT="$error_excerpt"

python - <<'PY'
import json, os

out = {
  "status": "skipped",
  "skip_reason": "repo_not_supported",
  "exit_code": 0,
  "stage": "cpu",
  "task": "train",
  "command": "",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": {
    "dataset": {
      "path": os.environ.get("SCIMLOPSBENCH_CPU_DATASET_PATH",""),
      "source": os.environ.get("SCIMLOPSBENCH_CPU_DATASET_SOURCE",""),
      "version": os.environ.get("SCIMLOPSBENCH_CPU_DATASET_VERSION",""),
      "sha256": os.environ.get("SCIMLOPSBENCH_CPU_DATASET_SHA256",""),
    },
    "model": {
      "path": os.environ.get("SCIMLOPSBENCH_CPU_MODEL_PATH",""),
      "source": os.environ.get("SCIMLOPSBENCH_CPU_MODEL_SOURCE",""),
      "version": os.environ.get("SCIMLOPSBENCH_CPU_MODEL_VERSION",""),
      "sha256": os.environ.get("SCIMLOPSBENCH_CPU_MODEL_SHA256",""),
    },
  },
  "meta": {
    "python": "",
    "git_commit": os.environ.get("SCIMLOPSBENCH_CPU_GIT_COMMIT",""),
    "env_vars": {k: os.environ.get(k, "") for k in ["SCIMLOPSBENCH_REPORT","SCIMLOPSBENCH_PYTHON","CUDA_VISIBLE_DEVICES"] if os.environ.get(k) is not None},
    "decision_reason": os.environ.get("SCIMLOPSBENCH_CPU_DECISION_REASON",""),
  },
  "failure_category": "not_applicable",
  "error_excerpt": os.environ.get("SCIMLOPSBENCH_CPU_ERROR_EXCERPT",""),
}

open("build_output/cpu/results.json", "w", encoding="utf-8").write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
PY

exit 0
