#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/build_output/multi_gpu"
ASSETS_JSON="$REPO_ROOT/benchmark_assets/assets.json"
CACHE_DIR="$REPO_ROOT/benchmark_assets/cache"

mkdir -p "$OUT_DIR"
LOG_TXT="$OUT_DIR/log.txt"
RESULTS_JSON="$OUT_DIR/results.json"

timestamp_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ" || true)"
git_commit="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"

PYTHON_BOOTSTRAP=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BOOTSTRAP="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BOOTSTRAP="python"
else
  echo "ERROR: python/python3 not found in PATH" >&2
  exit 1
fi

report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_path=""
if [[ -f "$report_path" ]]; then
  python_path="$("$PYTHON_BOOTSTRAP" - <<PY 2>/dev/null || true
import json, pathlib
p = pathlib.Path(${report_path@Q})
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  raise SystemExit(1)
pp = data.get("python_path","")
print(pp if isinstance(pp,str) else "")
PY
)"
fi

if [[ -z "${python_path:-}" ]]; then
  cat >"$LOG_TXT" <<EOF
[multi_gpu] failure: missing_report
Missing/invalid report.json or python_path.
EOF
  "$PYTHON_BOOTSTRAP" - <<PY || true
import json, pathlib
assets_path = pathlib.Path(${ASSETS_JSON@Q})
assets = {
  "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
  "model": {"path": "", "source": "", "version": "", "sha256": ""},
}
if assets_path.exists():
  try:
    d = json.loads(assets_path.read_text(encoding="utf-8"))
    if isinstance(d, dict):
      assets = d
  except Exception:
    pass
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": assets,
  "meta": {
    "python": "",
    "git_commit": ${git_commit@Q},
    "env_vars": {},
    "decision_reason": "python_path resolution failed; cannot run multi-GPU stage.",
    "timestamp_utc": ${timestamp_utc@Q},
  },
  "failure_category": "missing_report",
  "error_excerpt": pathlib.Path(${LOG_TXT@Q}).read_text(encoding="utf-8", errors="replace"),
}
pathlib.Path(${RESULTS_JSON@Q}).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY
  exit 1
fi

# Detect GPU count (physical, before restricting CUDA_VISIBLE_DEVICES).
gpu_count="$("$python_path" - <<'PY' 2>/dev/null || true
try:
  import torch
  print(torch.cuda.device_count())
except Exception:
  print("")
PY
)"

if [[ -z "$gpu_count" || "$gpu_count" -lt 2 ]]; then
  cat >"$LOG_TXT" <<EOF
[multi_gpu] failure: insufficient_hardware
Detected GPU count: ${gpu_count:-unknown} (need >= 2)
EOF
  "$PYTHON_BOOTSTRAP" - <<PY || true
import json, pathlib
assets_path = pathlib.Path(${ASSETS_JSON@Q})
assets = {
  "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
  "model": {"path": "", "source": "", "version": "", "sha256": ""},
}
if assets_path.exists():
  try:
    d = json.loads(assets_path.read_text(encoding="utf-8"))
    if isinstance(d, dict):
      assets = d
  except Exception:
    pass
payload = {
  "status": "failure",
  "skip_reason": "insufficient_hardware",
  "exit_code": 1,
  "stage": "multi_gpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": assets,
  "meta": {
    "python": ${python_path@Q},
    "git_commit": ${git_commit@Q},
    "env_vars": {},
    "decision_reason": "Need >=2 GPUs for tensor parallel inference (tensor_parallel_size=2).",
    "timestamp_utc": ${timestamp_utc@Q},
    "detected_gpu_count": ${gpu_count:-0},
  },
  "failure_category": "insufficient_hardware",
  "error_excerpt": pathlib.Path(${LOG_TXT@Q}).read_text(encoding="utf-8", errors="replace"),
}
pathlib.Path(${RESULTS_JSON@Q}).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY
  exit 1
fi

visible_devices="${SCIMLOPSBENCH_MULTI_GPU_VISIBLE_DEVICES:-0,1}"

PYCODE="$(cat <<'PY'
import json

from nanovllm import LLM, SamplingParams

assets = json.load(open("benchmark_assets/assets.json", "r", encoding="utf-8"))
model_path = assets["model"]["path"]
dataset_path = assets["dataset"]["path"]
dataset = json.load(open(dataset_path, "r", encoding="utf-8"))
prompt = dataset["prompts"][0]

llm = LLM(
    model_path,
    enforce_eager=True,
    tensor_parallel_size=2,
    max_model_len=256,
    max_num_batched_tokens=512,
    max_num_seqs=1,
)
sp = SamplingParams(temperature=0.6, max_tokens=1)
out = llm.generate([prompt], sp, use_tqdm=False)
print(out[0]["text"])
PY
)"

"$PYTHON_BOOTSTRAP" "$REPO_ROOT/benchmark_scripts/runner.py" \
  --stage multi_gpu \
  --task infer \
  --framework pytorch \
  --timeout-sec 1200 \
  --assets-json "$ASSETS_JSON" \
  --decision-reason "Run Nano-vLLM tensor-parallel inference (tensor_parallel_size=2) via the documented API; CUDA_VISIBLE_DEVICES restricted to two GPUs." \
  --env CUDA_VISIBLE_DEVICES="$visible_devices" \
  --env HF_HOME="$CACHE_DIR/huggingface" \
  --env HF_HUB_CACHE="$CACHE_DIR/huggingface/hub" \
  --env TRANSFORMERS_CACHE="$CACHE_DIR/transformers" \
  --env XDG_CACHE_HOME="$CACHE_DIR/xdg" \
  --env TORCH_HOME="$CACHE_DIR/torch" \
  --env TRITON_CACHE_DIR="$CACHE_DIR/triton" \
  -- python -c "$PYCODE"
