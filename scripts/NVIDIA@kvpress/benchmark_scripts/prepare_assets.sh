#!/usr/bin/env bash
set -u -o pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + model) under benchmark_assets/, using Hugging Face by default.

Outputs (always):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Side effects (on success):
  benchmark_assets/manifest.json
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Options:
  --python <path>           Override python executable (highest priority)
  --report-path <path>      Override agent report path (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  --dataset <name>          Dataset registry key (default: loogle)
  --data-dir <name>         Dataset data_dir/config (default: shortdep_qa)
  --model <repo_id>         HF model repo id (default: hf-internal-testing/tiny-random-LlamaForCausalLM)
  --revision <rev>          HF revision (default: main)
  --max-new-tokens <int>    Default: 1
  --max-context-length <n>  Default: 128
  --press-name <name>       Default: knorm
  --compression-ratio <f>   Default: 0.5
  --seed <int>              Default: 42
  --offline                 Force offline mode (requires existing cache)
EOF
}

python_bin=""
report_path=""
dataset_name="loogle"
data_dir="shortdep_qa"
model_repo="hf-internal-testing/tiny-random-LlamaForCausalLM"
revision="main"
max_new_tokens="1"
max_context_length="128"
press_name="knorm"
compression_ratio="0.5"
seed="42"
offline=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_bin="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --dataset)
      dataset_name="${2:-}"; shift 2 ;;
    --data-dir)
      data_dir="${2:-}"; shift 2 ;;
    --model)
      model_repo="${2:-}"; shift 2 ;;
    --revision)
      revision="${2:-}"; shift 2 ;;
    --max-new-tokens)
      max_new_tokens="${2:-}"; shift 2 ;;
    --max-context-length)
      max_context_length="${2:-}"; shift 2 ;;
    --press-name)
      press_name="${2:-}"; shift 2 ;;
    --compression-ratio)
      compression_ratio="${2:-}"; shift 2 ;;
    --seed)
      seed="${2:-}"; shift 2 ;;
    --offline)
      offline=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2 ;;
  esac
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"

stage="prepare"
out_dir="$repo_root/build_output/$stage"
log_path="$out_dir/log.txt"
results_json="$out_dir/results.json"

assets_root="$repo_root/benchmark_assets"
cache_root="$assets_root/cache"
dataset_root="$assets_root/dataset"
model_root="$assets_root/model"
manifest_path="$assets_root/manifest.json"

mkdir -p "$out_dir" "$cache_root" "$dataset_root" "$model_root"
: >"$log_path"

log() { printf '%s\n' "$*" | tee -a "$log_path" >/dev/null; }

resolve_report_path() {
  if [[ -n "$report_path" ]]; then
    printf '%s' "$report_path"
  elif [[ -n "${SCIMLOPSBENCH_REPORT:-}" ]]; then
    printf '%s' "$SCIMLOPSBENCH_REPORT"
  else
    printf '%s' "/opt/scimlopsbench/report.json"
  fi
}

resolve_python_path() {
  if [[ -n "$python_bin" ]]; then
    printf '%s' "$python_bin"
    return 0
  fi
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    printf '%s' "$SCIMLOPSBENCH_PYTHON"
    return 0
  fi
  local rp
  rp="$(resolve_report_path)"
  if [[ ! -f "$rp" ]]; then
    return 1
  fi
  python3 - <<PY 2>/dev/null
import json
try:
  d = json.load(open("$rp", "r", encoding="utf-8"))
  print(d.get("python_path","") or "")
except Exception:
  print("")
PY
}

python_path="$(resolve_python_path || true)"
report_p="$(resolve_report_path)"

status="failure"
exit_code=1
failure_category="unknown"
skip_reason="not_applicable"
decision_reason="Using documented evaluation dataset (loogle/shortdep_qa) and a tiny public Llama model for fast anonymous downloads."
cmd_str=""

if [[ -z "$python_path" ]]; then
  log "ERROR: Could not resolve python (missing/invalid report: $report_p)."
  failure_category="missing_report"
else
  log "Repo root: $repo_root"
  log "Using python: $python_path"
  log "Report path: $report_p"
  log "Dataset: $dataset_name (data_dir: $data_dir)"
  log "Model: $model_repo (revision: $revision)"
  log "offline: $offline"

  export PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}"
  export HF_HOME="$cache_root/hf_home"
  export HF_HUB_CACHE="$cache_root/hf_hub"
  export HUGGINGFACE_HUB_CACHE="$cache_root/hf_hub"
  export TRANSFORMERS_CACHE="$cache_root/transformers"
  export HF_DATASETS_CACHE="$cache_root/datasets"
  export XDG_CACHE_HOME="$cache_root/xdg"
  export TOKENIZERS_PARALLELISM=false
  if [[ "$offline" -eq 1 ]]; then
    export HF_HUB_OFFLINE=1
    export HF_DATASETS_OFFLINE=1
  fi

  cmd_str="$python_path - <<PY (inline downloader)"
  "$python_path" - <<PY >>"$log_path" 2>&1
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path("$repo_root").resolve()
ASSETS_ROOT = REPO_ROOT / "benchmark_assets"
CACHE_ROOT = ASSETS_ROOT / "cache"
DATASET_ROOT = ASSETS_ROOT / "dataset"
MODEL_ROOT = ASSETS_ROOT / "model"
MANIFEST_PATH = ASSETS_ROOT / "manifest.json"

DATASET_NAME = "$dataset_name"
DATA_DIR = "$data_dir"
MODEL_REPO = "$model_repo"
REVISION = "$revision"

PRESS_NAME = "$press_name"
COMPRESSION_RATIO = float("$compression_ratio")
MAX_NEW_TOKENS = int("$max_new_tokens")
MAX_CONTEXT_LENGTH = int("$max_context_length")
SEED = int("$seed")

def _sanitize(s: str) -> str:
  return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)

def _ensure_dir(p: Path) -> None:
  p.mkdir(parents=True, exist_ok=True)

def _write_json(p: Path, obj) -> None:
  _ensure_dir(p.parent)
  p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def _sha256_text(text: str) -> str:
  import hashlib
  h = hashlib.sha256()
  h.update(text.encode("utf-8"))
  return h.hexdigest()

def _dir_hash_manifest(paths: list[Path]) -> tuple[str, list[dict]]:
  files = []
  for fp in paths:
    try:
      files.append({"path": str(fp), "size_bytes": fp.stat().st_size})
    except OSError:
      files.append({"path": str(fp), "size_bytes": None})
  payload = json.dumps(files, sort_keys=True, ensure_ascii=False)
  return _sha256_text(payload), files

def _load_dataset_registry() -> dict:
  # Use evaluation registry if available; fall back to embedded mapping.
  try:
    sys.path.insert(0, str(REPO_ROOT / "evaluation"))
    from evaluate_registry import DATASET_REGISTRY  # type: ignore

    return dict(DATASET_REGISTRY)
  except Exception:
    return {
      "loogle": "simonjegou/loogle",
      "ruler": "simonjegou/ruler",
      "zero_scrolls": "simonjegou/zero_scrolls",
      "infinitebench": "MaxJeblick/InfiniteBench",
      "longbench": "Xnhyacinth/LongBench",
      "longbench-e": "Xnhyacinth/LongBench",
      "longbench-v2": "simonjegou/LongBench-v2",
      "needle_in_haystack": "alessiodevoto/paul_graham_essays",
      "aime25": "alessiodevoto/aime25",
      "math500": "alessiodevoto/math500",
    }

def main() -> int:
  dataset_registry = _load_dataset_registry()
  if DATASET_NAME not in dataset_registry:
    print(f"ERROR: dataset '{DATASET_NAME}' not in DATASET_REGISTRY", file=sys.stderr)
    return 2

  # --- Model download (to cache, then link into benchmark_assets/model/) ---
  model_cache_dir = CACHE_ROOT / "models" / _sanitize(MODEL_REPO) / _sanitize(REVISION)
  _ensure_dir(model_cache_dir)

  model_ok = (model_cache_dir / "config.json").exists()
  if not model_ok:
    try:
      from huggingface_hub import snapshot_download
    except Exception as e:
      print(f"ERROR: huggingface_hub not available: {e}", file=sys.stderr)
      return 3
    print(f"Downloading model to: {model_cache_dir}")
    snapshot_download(
      repo_id=MODEL_REPO,
      revision=REVISION,
      local_dir=str(model_cache_dir),
      local_dir_use_symlinks=False,
    )
  if not (model_cache_dir / "config.json").exists():
    print(f"ERROR: model download appears complete but config.json not found under: {model_cache_dir}", file=sys.stderr)
    return 4

  model_link_dir = MODEL_ROOT / _sanitize(MODEL_REPO)
  try:
    if model_link_dir.is_symlink() or model_link_dir.exists():
      if model_link_dir.is_symlink():
        model_link_dir.unlink()
      else:
        shutil.rmtree(model_link_dir)
    model_link_dir.symlink_to(model_cache_dir, target_is_directory=True)
  except OSError:
    # Fallback to copy if symlinks unsupported.
    if model_link_dir.exists():
      shutil.rmtree(model_link_dir)
    shutil.copytree(model_cache_dir, model_link_dir)

  # --- Dataset download (populate datasets cache + record cache files) ---
  hf_dataset_id = dataset_registry[DATASET_NAME]
  dataset_cache_dir = CACHE_ROOT / "datasets"
  _ensure_dir(dataset_cache_dir)

  # Load dataset to ensure it is available offline later and to compute N for fraction=1/N.
  try:
    from datasets import load_dataset
  except Exception as e:
    print(f"ERROR: datasets not available: {e}", file=sys.stderr)
    return 5

  print(f"Loading dataset via datasets.load_dataset: {hf_dataset_id} (data_dir={DATA_DIR})")
  try:
    ds = load_dataset(hf_dataset_id, data_dir=DATA_DIR or None, split="test", cache_dir=str(dataset_cache_dir))
  except Exception as e:
    print(f"ERROR: dataset download/load failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 6

  try:
    n_rows = len(ds)  # type: ignore[arg-type]
  except Exception:
    n_rows = None

  cache_files = []
  try:
    for item in getattr(ds, "cache_files", []) or []:  # type: ignore[attr-defined]
      fn = item.get("filename") if isinstance(item, dict) else None
      if fn:
        cache_files.append(Path(fn))
  except Exception:
    cache_files = []

  dataset_asset_dir = DATASET_ROOT / _sanitize(f"{DATASET_NAME}__{DATA_DIR or 'default'}")
  _ensure_dir(dataset_asset_dir)

  sha, files_manifest = _dir_hash_manifest(cache_files)
  _write_json(dataset_asset_dir / "cache_files.json", files_manifest)

  # One-sample fraction: ensure >0 and round up slightly to avoid float edge cases.
  if not n_rows or n_rows <= 0:
    print("ERROR: dataset length could not be determined (n_rows is 0/None)", file=sys.stderr)
    return 7
  frac = min(1.0, (1.0 + 1e-6) / float(n_rows))

  manifest = {
    "dataset": {
      "name": DATASET_NAME,
      "hf_id": hf_dataset_id,
      "data_dir": DATA_DIR,
      "path": str(dataset_asset_dir),
      "source": f"hf://datasets/{hf_dataset_id}",
      "version": "main",
      "sha256": sha,
      "prepared": {
        "split": "test",
        "num_rows": int(n_rows),
        "fraction_for_one_sample": frac,
      },
      "cache": {
        "hf_home": str(Path(os.environ.get("HF_HOME",""))),
        "hf_datasets_cache": str(Path(os.environ.get("HF_DATASETS_CACHE",""))),
      },
    },
    "model": {
      "repo_id": MODEL_REPO,
      "revision": REVISION,
      "path": str(model_link_dir),
      "source": f"hf://models/{MODEL_REPO}",
      "version": REVISION,
      "sha256": sha,  # placeholder; filled below
    },
    "evaluation": {
      "entrypoint": "evaluation/evaluate.py",
      "press_name": PRESS_NAME,
      "compression_ratio": COMPRESSION_RATIO,
      "max_new_tokens": MAX_NEW_TOKENS,
      "max_context_length": MAX_CONTEXT_LENGTH,
      "seed": SEED,
      "fraction": frac,
      "steps": 1,
      "batch_size": 1,
    },
  }

  # Compute model sha after link/copy.
  try:
    sys.path.insert(0, str(REPO_ROOT / "benchmark_scripts"))
    from bench_utils import sha256_dir  # type: ignore

    manifest["model"]["sha256"] = sha256_dir(model_link_dir)
  except Exception:
    manifest["model"]["sha256"] = ""

  _write_json(MANIFEST_PATH, manifest)
  print(f"Wrote manifest: {MANIFEST_PATH}")
  return 0

if __name__ == "__main__":
  raise SystemExit(main())
PY
  rc="$?"

  if [[ "$rc" -eq 0 && -f "$manifest_path" ]]; then
    status="success"
    exit_code=0
    failure_category="not_applicable"
  else
    status="failure"
    exit_code=1
    if [[ "$rc" -eq 2 ]]; then
      failure_category="args_unknown"
    elif [[ "$rc" -eq 3 || "$rc" -eq 5 ]]; then
      failure_category="deps"
    elif [[ "$rc" -eq 6 ]]; then
      failure_category="download_failed"
    elif [[ "$rc" -eq 4 || "$rc" -eq 7 ]]; then
      failure_category="model"
    else
      failure_category="unknown"
    fi
  fi
fi

dataset_path=""
dataset_source=""
dataset_version=""
dataset_sha=""
model_path=""
model_source=""
model_version=""
model_sha=""
if [[ -f "$manifest_path" ]]; then
  dataset_path="$(python3 -c "import json;print(json.load(open('$manifest_path'))['dataset'].get('path',''))" 2>/dev/null || true)"
  dataset_source="$(python3 -c "import json;print(json.load(open('$manifest_path'))['dataset'].get('source',''))" 2>/dev/null || true)"
  dataset_version="$(python3 -c "import json;print(json.load(open('$manifest_path'))['dataset'].get('version',''))" 2>/dev/null || true)"
  dataset_sha="$(python3 -c "import json;print(json.load(open('$manifest_path'))['dataset'].get('sha256',''))" 2>/dev/null || true)"
  model_path="$(python3 -c "import json;print(json.load(open('$manifest_path'))['model'].get('path',''))" 2>/dev/null || true)"
  model_source="$(python3 -c "import json;print(json.load(open('$manifest_path'))['model'].get('source',''))" 2>/dev/null || true)"
  model_version="$(python3 -c "import json;print(json.load(open('$manifest_path'))['model'].get('version',''))" 2>/dev/null || true)"
  model_sha="$(python3 -c "import json;print(json.load(open('$manifest_path'))['model'].get('sha256',''))" 2>/dev/null || true)"
fi

git_commit=""
git_commit="$(cd "$repo_root" && git rev-parse HEAD 2>/dev/null || true)"

python3 - <<PY >"$results_json" 2>>"$log_path"
import json, os, pathlib

def tail_lines(path: pathlib.Path, max_lines: int = 220) -> str:
  try:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:]).strip()
  except Exception:
    return ""

payload = {
  "status": "$status",
  "skip_reason": "$skip_reason",
  "exit_code": int("$exit_code"),
  "stage": "prepare",
  "task": "download",
  "command": "$cmd_str",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "$dataset_path", "source": "$dataset_source", "version": "$dataset_version", "sha256": "$dataset_sha"},
    "model": {"path": "$model_path", "source": "$model_source", "version": "$model_version", "sha256": "$model_sha"},
  },
  "meta": {
    "python": "$python_path",
    "git_commit": "$git_commit",
    "env_vars": {k: os.environ.get(k,"") for k in [
      "HF_HOME","HF_HUB_CACHE","HF_DATASETS_CACHE","TRANSFORMERS_CACHE","XDG_CACHE_HOME","HF_HUB_OFFLINE","HF_DATASETS_OFFLINE","TOKENIZERS_PARALLELISM"
    ] if os.environ.get(k)},
    "decision_reason": "$decision_reason",
    "report_path": "$report_p",
  },
  "failure_category": "$failure_category",
  "error_excerpt": tail_lines(pathlib.Path("$log_path")),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

exit "$exit_code"
