#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root" || exit 1

export PYTHONDONTWRITEBYTECODE=1

stage="cpu"
out_dir="build_output/$stage"
mkdir -p "$out_dir"

# Force CPU.
export CUDA_VISIBLE_DEVICES=""

# Redirect common caches into allowed tree.
export XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg"
export HF_HOME="$repo_root/benchmark_assets/cache/hf_home"
export HF_HUB_CACHE="$repo_root/benchmark_assets/cache/hf_hub"
export HUGGINGFACE_HUB_CACHE="$repo_root/benchmark_assets/cache/hf_hub"
export HF_DATASETS_CACHE="$repo_root/benchmark_assets/cache/hf_datasets"
export TRANSFORMERS_CACHE="$repo_root/benchmark_assets/cache/hf_transformers"
export TORCH_HOME="$repo_root/benchmark_assets/cache/torch"
export MPLCONFIGDIR="$repo_root/benchmark_assets/cache/matplotlib"
export PIP_CACHE_DIR="$repo_root/benchmark_assets/cache/pip"
export PIP_DISABLE_PIP_VERSION_CHECK=1

# Best-effort: set SCIMLOPSBENCH_PYTHON from report.json if available.
if [[ -z "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
  if [[ -n "$report_path" && -d "$report_path" ]]; then
    report_path="$report_path/report.json"
  fi
  pyhost="$(command -v python3 || command -v python || true)"
  if [[ -n "$pyhost" && -f "$report_path" ]]; then
    set +u
    resolved="$("$pyhost" - <<'PY' 2>/dev/null || true
import json
import os
from pathlib import Path

raw = os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"
p = Path(raw)
if p.is_dir():
    p = p / "report.json"
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("python_path", ""))
except Exception:
    print("")
PY
)"
    set -u
    resolved="${resolved//$'\r'/}"
    if [[ -n "$resolved" ]]; then
      export SCIMLOPSBENCH_PYTHON="$resolved"
    fi
  fi
fi

PYBIN="${SCIMLOPSBENCH_PYTHON:-}"
if [[ -n "$PYBIN" && ! -x "$PYBIN" ]]; then
  PYBIN=""
fi
if [[ -z "$PYBIN" ]]; then
  PYBIN="$(command -v python3 || command -v python)"
fi

"$PYBIN" benchmark_scripts/runner.py \
  --stage "$stage" --task train --framework pytorch \
  --status skipped --skip-reason repo_not_supported --failure-category cpu_not_supported \
  --decision-reason "CPU stage intentionally skipped (user policy). Repo training entrypoint scripts/train_voxcpm_finetune.py enables AMP with a CUDA-only autocast context and casts inputs to bfloat16, which leads to dtype mismatch on CPU in practice." \
  --message "Skipping CPU run by policy (repo_not_supported). Evidence: scripts/train_voxcpm_finetune.py uses Accelerator(amp=True) and accelerator.autocast(dtype=bfloat16); accelerator.autocast is implemented as torch.amp.autocast('cuda', ...) (no CPU autocast), so CPU runs hit BF16/FP32 matmul dtype mismatch." \
  --command-str "python scripts/train_voxcpm_finetune.py --config_path build_output/$stage/config.yaml"

exit $?
