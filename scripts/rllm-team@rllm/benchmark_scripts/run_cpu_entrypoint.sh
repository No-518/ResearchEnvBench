#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal 1-step CPU training run via the repository entrypoint.

Entrypoint:
  examples/rdl.py (RelBench Rel-F1)

Writes:
  build_output/cpu/log.txt
  build_output/cpu/results.json

Options:
  --python <path>        Explicit python executable (overrides report/env)
  --report-path <path>   Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --timeout-sec <int>    Default: 600
EOF
}

python_bin=""
report_path=""
timeout_sec="600"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
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

# Prevent __pycache__ in the repo and keep caches in benchmark_assets/cache.
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export BENCHMARK_ASSETS_DIR="$repo_root/benchmark_assets"
export XDG_CACHE_HOME="$BENCHMARK_ASSETS_DIR/cache/xdg"
export PIP_CACHE_DIR="$BENCHMARK_ASSETS_DIR/cache/pip"
export HF_HOME="$BENCHMARK_ASSETS_DIR/cache/huggingface"
export TRANSFORMERS_CACHE="$BENCHMARK_ASSETS_DIR/cache/huggingface/transformers"
export HF_DATASETS_CACHE="$BENCHMARK_ASSETS_DIR/cache/huggingface/datasets"
export TORCH_HOME="$BENCHMARK_ASSETS_DIR/cache/torch"
export SENTENCE_TRANSFORMERS_HOME="$BENCHMARK_ASSETS_DIR/cache/sentence_transformers"
export TMPDIR="$BENCHMARK_ASSETS_DIR/cache/tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
mkdir -p "$XDG_CACHE_HOME" "$PIP_CACHE_DIR" "$HF_HOME" "$TRANSFORMERS_CACHE" "$HF_DATASETS_CACHE" "$TORCH_HOME" "$SENTENCE_TRANSFORMERS_HOME" "$TMPDIR"

# Force CPU visibility.
export CUDA_VISIBLE_DEVICES=""

prepare_results="$repo_root/build_output/prepare/results.json"
dataset_dir="$repo_root/benchmark_assets/dataset"
if [[ -f "$prepare_results" ]]; then
  # Best-effort: use dataset path recorded by prepare stage.
  parse_py="${python_bin:-python}"
  dataset_dir="$("$parse_py" - <<'PY' 2>/dev/null || true
import json, pathlib
p = pathlib.Path("build_output/prepare/results.json")
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print(data.get("assets", {}).get("dataset", {}).get("path", ""))
except Exception:
    pass
PY
)"
  [[ -n "$dataset_dir" ]] || dataset_dir="$repo_root/benchmark_assets/dataset"
fi

decision_reason="Use examples/rdl.py because it is an official repo example with configurable --cache_dir; use task driver-position to avoid BatchNorm1d(out_dim=1) failing at batch_size=1; force CPU via CUDA_VISIBLE_DEVICES=\"\"; enforce batch_size=1, epochs=1, max_steps_per_epoch=1."

runner_py="${python_bin:-python}"
set +e
"$runner_py" "$repo_root/benchmark_scripts/runner.py" run \
  --stage cpu \
  --task train \
  --framework pytorch \
  --timeout-sec "$timeout_sec" \
  --report-path "$report_path" \
  ${python_bin:+--python "$python_bin"} \
  --requires-python \
  --assets-json "$prepare_results" \
  --decision-reason "$decision_reason" \
  -- \
  "{python}" "examples/rdl.py" \
    --task "driver-position" \
    --epochs 1 \
    --batch_size 1 \
    --num_neighbors 1 \
    --num_layers 1 \
    --hidden_dim 16 \
    --max_steps_per_epoch 1 \
    --cache_dir "$dataset_dir"
rc=$?
set -e
exit "$rc"
