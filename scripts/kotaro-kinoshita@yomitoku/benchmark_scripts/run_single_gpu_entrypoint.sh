#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal single-GPU inference using the repository entrypoint.

Outputs:
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json
EOF
}

timeout_sec="${SCIMLOPSBENCH_SINGLE_GPU_TIMEOUT_SEC:-600}"
report_path=""
python_bin=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PREP_RESULTS="$REPO_ROOT/build_output/prepare/results.json"
DATASET_DEFAULT="$REPO_ROOT/benchmark_assets/dataset/sample_text.jpg"

dataset_path="$DATASET_DEFAULT"
decision_reason="Using YomiToku CLI (yomitoku.cli.main) for minimal single-GPU inference; forcing GPU via --device cuda and CUDA_VISIBLE_DEVICES=0."

if [[ -f "$PREP_RESULTS" ]]; then
  set +e
  parsed="$(
    python - <<PY
import json
from pathlib import Path
try:
    obj=json.loads(Path(r"""$PREP_RESULTS""").read_text(encoding="utf-8"))
    ds=obj.get("assets",{}).get("dataset",{}).get("path","")
    print(ds)
except Exception:
    pass
PY
  )"
  set -e
  if [[ -n "${parsed:-}" ]]; then
    dataset_path="$parsed"
    decision_reason="$decision_reason Dataset path from build_output/prepare/results.json."
  else
    decision_reason="$decision_reason WARNING: Could not parse dataset path from prepare results; using default path."
  fi
else
  decision_reason="$decision_reason WARNING: Missing build_output/prepare/results.json; using default dataset path."
fi

HF_HOME_DIR="$REPO_ROOT/benchmark_assets/cache/huggingface"

# Make the CUDA precheck consistent with the intended single-GPU execution.
export CUDA_VISIBLE_DEVICES="0"

# Pre-check: skip if no CUDA device in the reported python environment.
PY_USED=""
if [[ -n "$python_bin" ]]; then
  PY_USED="$python_bin"
else
  set +e
  if [[ -n "$report_path" ]]; then
    PY_USED="$(python "$REPO_ROOT/benchmark_scripts/runner.py" resolve-python --report-path "$report_path" 2>/dev/null)"
  else
    PY_USED="$(python "$REPO_ROOT/benchmark_scripts/runner.py" resolve-python 2>/dev/null)"
  fi
  set -e
fi

cuda_ok="unknown"
gpu_count="0"
if [[ -n "$PY_USED" ]]; then
  set +e
  cuda_line="$("$PY_USED" - <<'PY'
try:
    import torch
    print("1" if torch.cuda.is_available() else "0")
    print(str(torch.cuda.device_count()))
except Exception:
    print("unknown")
    print("0")
PY
)"
  set -e
  cuda_ok="$(echo "$cuda_line" | head -n1 | tr -d '\r')"
  gpu_count="$(echo "$cuda_line" | tail -n1 | tr -d '\r')"
fi

runner_common=(
  run
  --stage single_gpu
  --task infer
  --out-dir build_output/single_gpu
  --timeout-sec "$timeout_sec"
  --framework pytorch
  --requires-python
  --decision-reason "$decision_reason"
  --assets-from build_output/prepare/results.json
  --env "HF_HOME=$HF_HOME_DIR"
  --env "HUGGINGFACE_HUB_CACHE=$HF_HOME_DIR/hub"
  --env "HF_HUB_DISABLE_TELEMETRY=1"
  --env "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
  --env "PYTHONPATH=$REPO_ROOT/src"
)
if [[ -n "$python_bin" ]]; then
  runner_common+=(--python "$python_bin")
fi
if [[ -n "$report_path" ]]; then
  runner_common+=(--report-path "$report_path")
fi

if [[ "$cuda_ok" != "1" || "${gpu_count:-0}" -lt 1 ]]; then
  python "$REPO_ROOT/benchmark_scripts/runner.py" "${runner_common[@]}" \
    -- bash -lc "echo \"insufficient_hardware: CUDA not available or no visible GPU (cuda_ok=$cuda_ok, gpu_count=$gpu_count, CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES)\"; exit 1"
  exit 1
fi

python "$REPO_ROOT/benchmark_scripts/runner.py" "${runner_common[@]}" -- \
  "{python}" -m yomitoku.cli.main \
  "$dataset_path" \
  --format json \
  --outdir "build_output/single_gpu/out" \
  --device cuda
