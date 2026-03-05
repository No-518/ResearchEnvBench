#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

stage="cpu"
out_dir="$REPO_ROOT/build_output/$stage"
mkdir -p "$out_dir"

cache_root="$REPO_ROOT/benchmark_assets/cache"
export HF_HOME="$cache_root/huggingface"
export HF_HUB_CACHE="$HF_HOME/hub"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export XDG_CACHE_HOME="$cache_root/xdg"
export TORCH_HOME="$cache_root/torch"

prepare_results="$REPO_ROOT/build_output/prepare/results.json"
dataset_path="$REPO_ROOT/benchmark_assets/dataset/ragbench_min.json"
model_path=""

if [[ -f "$prepare_results" ]]; then
  dataset_path="$(python - <<'PY' "$prepare_results" "$dataset_path" 2>/dev/null
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
fallback = sys.argv[2]
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    v = (data.get("assets", {}).get("dataset", {}) or {}).get("path")
    print(v if isinstance(v, str) and v else fallback)
except Exception:
    print(fallback)
PY
  )"
  model_path="$(python - <<'PY' "$prepare_results" 2>/dev/null || true
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    v = (data.get("assets", {}).get("model", {}) or {}).get("path")
    print(v if isinstance(v, str) else "")
except Exception:
    print("")
PY
  )"
fi

if [[ -z "$model_path" ]]; then
  # Fallback: pick first entry under benchmark_assets/model/
  model_path="$(ls -1 "$REPO_ROOT/benchmark_assets/model" 2>/dev/null | head -n 1 || true)"
  if [[ -n "$model_path" ]]; then
    model_path="$REPO_ROOT/benchmark_assets/model/$model_path"
  fi
fi

ragtruth_empty="$REPO_ROOT/benchmark_assets/dataset/ragtruth_empty.json"

decision_reason="Use official repo entrypoint scripts/train.py with a 2-sample mini dataset (1 train + 1 dev) derived from RAGTruth; force CPU via CUDA_VISIBLE_DEVICES=\"\"; batch_size=1, epochs=1 yields 1 optimizer step."

if [[ ! -f "$dataset_path" || ! -f "$ragtruth_empty" ]]; then
  python "$REPO_ROOT/benchmark_scripts/runner.py" \
    --stage "$stage" --task train --framework pytorch \
    --assets-from "$prepare_results" \
    --dataset-path "$dataset_path" --model-path "$model_path" \
    --failure-category data \
    --decision-reason "$decision_reason" \
    --requires-python \
    -- -- bash -lc "echo 'Missing dataset assets; run benchmark_scripts/prepare_assets.sh first.'; exit 1"
  exit 1
fi
if [[ -z "$model_path" || ! -d "$model_path" ]]; then
  python "$REPO_ROOT/benchmark_scripts/runner.py" \
    --stage "$stage" --task train --framework pytorch \
    --assets-from "$prepare_results" \
    --dataset-path "$dataset_path" --model-path "$model_path" \
    --failure-category model \
    --decision-reason "$decision_reason" \
    --requires-python \
    -- -- bash -lc "echo 'Missing model assets; run benchmark_scripts/prepare_assets.sh first.'; exit 1"
  exit 1
fi

set +e
python "$REPO_ROOT/benchmark_scripts/runner.py" \
  --stage "$stage" --task train --framework pytorch \
  --assets-from "$prepare_results" \
  --dataset-path "$dataset_path" --model-path "$model_path" \
  --decision-reason "$decision_reason" \
  --env "CUDA_VISIBLE_DEVICES=" \
  --env "HF_HOME=$HF_HOME" \
  --env "HF_HUB_CACHE=$HF_HUB_CACHE" \
  --env "HUGGINGFACE_HUB_CACHE=$HUGGINGFACE_HUB_CACHE" \
  --env "TRANSFORMERS_CACHE=$TRANSFORMERS_CACHE" \
  --env "XDG_CACHE_HOME=$XDG_CACHE_HOME" \
  --env "TORCH_HOME=$TORCH_HOME" \
  --requires-python \
  --python-script "scripts/train.py" -- \
  --ragtruth-path "$ragtruth_empty" \
  --ragbench-path "$dataset_path" \
  --model-name "$model_path" \
  --output-dir "$out_dir/model" \
  --batch-size 1 \
  --epochs 1 \
  --learning-rate 1e-5
ec=$?
set -e

# If run succeeded, confirm it actually used CPU.
if [[ $ec -eq 0 ]]; then
  if ! python - <<'PY' "$out_dir/log.txt"
import sys
from pathlib import Path
p = Path(sys.argv[1])
txt = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
sys.exit(0 if "Starting training on cpu" in txt else 1)
PY
  then
    python - <<'PY' "$out_dir/results.json" "$out_dir/log.txt"
import json, sys
from pathlib import Path
rp = Path(sys.argv[1])
lp = Path(sys.argv[2])
data = json.loads(rp.read_text(encoding="utf-8"))
data["status"] = "failure"
data["exit_code"] = 1
data["failure_category"] = "unknown"
data["error_excerpt"] = "\n".join(lp.read_text(encoding="utf-8", errors="replace").splitlines()[-220:])
data["meta"]["cpu_force_check"] = "Expected 'Starting training on cpu' in log but did not find it"
rp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
    exit 1
  fi
fi

exit "$ec"
