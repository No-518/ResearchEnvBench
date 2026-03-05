#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal CPU inference using the repository entrypoint.

Outputs:
  build_output/cpu/log.txt
  build_output/cpu/results.json
EOF
}

timeout_sec="${SCIMLOPSBENCH_CPU_TIMEOUT_SEC:-600}"
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
decision_reason="Using YomiToku CLI (yomitoku.cli.main) for minimal CPU inference; forcing CPU via --device cpu and CUDA_VISIBLE_DEVICES=\"\"."

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

runner_args=(
  run
  --stage cpu
  --task infer
  --out-dir build_output/cpu
  --timeout-sec "$timeout_sec"
  --framework pytorch
  --requires-python
  --decision-reason "$decision_reason"
  --assets-from build_output/prepare/results.json
  --env "CUDA_VISIBLE_DEVICES="
  --env "HF_HOME=$HF_HOME_DIR"
  --env "HUGGINGFACE_HUB_CACHE=$HF_HOME_DIR/hub"
  --env "HF_HUB_DISABLE_TELEMETRY=1"
  --env "PYTHONPATH=$REPO_ROOT/src"
)

if [[ -n "$python_bin" ]]; then
  runner_args+=(--python "$python_bin")
fi
if [[ -n "$report_path" ]]; then
  runner_args+=(--report-path "$report_path")
fi

python "$REPO_ROOT/benchmark_scripts/runner.py" "${runner_args[@]}" -- \
  "{python}" -m yomitoku.cli.main \
  "$dataset_path" \
  --format json \
  --outdir "build_output/cpu/out" \
  --device cpu

