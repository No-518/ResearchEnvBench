#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + model) in a reproducible, cacheable way.

Outputs (always written, even on failure):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Writes assets to:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/
  benchmark_assets/assets.json

Options:
  --python <path>          Override Python interpreter (else uses report.json python_path)
  --report-path <path>     Override agent report path (default: /opt/scimlopsbench/report.json or $SCIMLOPSBENCH_REPORT)
  --model-id <id>          Default: Qwen/Qwen3-0.6B
  --model-revision <rev>   Default: (HF default, usually 'main')
  --offline               Force offline mode (use cache/local files only)
EOF
}

python_override=""
report_path=""
model_id="Qwen/Qwen3-0.6B"
model_revision=""
offline=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_override="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --model-id)
      model_id="${2:-}"; shift 2 ;;
    --model-revision)
      model_revision="${2:-}"; shift 2 ;;
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/build_output/prepare"
ASSETS_DIR="$REPO_ROOT/benchmark_assets"
CACHE_DIR="$ASSETS_DIR/cache"
DATASET_DIR="$ASSETS_DIR/dataset"
MODEL_DIR="$ASSETS_DIR/model"
ASSETS_JSON="$ASSETS_DIR/assets.json"

mkdir -p "$OUT_DIR" "$CACHE_DIR" "$DATASET_DIR" "$MODEL_DIR"

LOG_TXT="$OUT_DIR/log.txt"
RESULTS_JSON="$OUT_DIR/results.json"

exec > >(tee "$LOG_TXT") 2>&1

PY_BOOTSTRAP=""
if command -v python3 >/dev/null 2>&1; then
  PY_BOOTSTRAP="python3"
elif command -v python >/dev/null 2>&1; then
  PY_BOOTSTRAP="python"
else
  echo "ERROR: python/python3 not found in PATH" >&2
  exit 1
fi

timestamp_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ" || true)"
git_commit="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"

resolve_report_path() {
  if [[ -n "$report_path" ]]; then
    echo "$report_path"
    return 0
  fi
  if [[ -n "${SCIMLOPSBENCH_REPORT:-}" ]]; then
    echo "$SCIMLOPSBENCH_REPORT"
    return 0
  fi
  echo "/opt/scimlopsbench/report.json"
}

resolve_python_from_report() {
  local rp="$1"
  if [[ ! -f "$rp" ]]; then
    return 1
  fi
  "$PY_BOOTSTRAP" - <<PY
import json, pathlib, sys
p = pathlib.Path(${rp@Q})
try:
  data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
  sys.exit(1)
pp = data.get("python_path")
if not isinstance(pp, str) or not pp.strip():
  sys.exit(1)
print(pp)
PY
}

python_path=""
python_resolution="unknown"
python_warning=""

if [[ -n "$python_override" ]]; then
  python_path="$python_override"
  python_resolution="cli"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  python_path="$SCIMLOPSBENCH_PYTHON"
  python_resolution="env:SCIMLOPSBENCH_PYTHON"
else
  rp="$(resolve_report_path)"
  if python_from_report="$(resolve_python_from_report "$rp" 2>/dev/null)"; then
    python_path="$python_from_report"
    python_resolution="report:python_path"
  else
    echo "ERROR: Missing/invalid report.json (provide --python or set SCIMLOPSBENCH_PYTHON/SCIMLOPSBENCH_REPORT)." >&2
    python_path=""
  fi
fi

write_results() {
  local status="$1"
  local failure_category="$2"
  local exit_code="$3"
  "$PY_BOOTSTRAP" - <<PY || true
import json, pathlib

repo_root = pathlib.Path(${REPO_ROOT@Q})
out_dir = pathlib.Path(${OUT_DIR@Q})
log_txt = out_dir / "log.txt"

def tail(p: pathlib.Path, n: int = 240) -> str:
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

payload = {
  "status": ${status@Q},
  "skip_reason": "unknown",
  "exit_code": int(${exit_code@Q}),
  "stage": "prepare",
  "task": "download",
  "command": ${python_path@Q} + " - <inline>",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": ${python_path@Q},
    "python_resolution": ${python_resolution@Q},
    "python_warning": ${python_warning@Q},
    "git_commit": ${git_commit@Q},
    "env_vars": {
      "HF_HOME": str(pathlib.Path(${CACHE_DIR@Q}) / "huggingface"),
      "TRANSFORMERS_CACHE": str(pathlib.Path(${CACHE_DIR@Q}) / "transformers"),
      "XDG_CACHE_HOME": str(pathlib.Path(${CACHE_DIR@Q}) / "xdg"),
    },
    "decision_reason": "Model from README.md: Qwen/Qwen3-0.6B; dataset is a minimal prompts JSON derived from example.py.",
    "timestamp_utc": ${timestamp_utc@Q},
  },
  "failure_category": ${failure_category@Q},
  "error_excerpt": tail(log_txt, 240),
}

assets_path = pathlib.Path(${ASSETS_JSON@Q})
if assets_path.exists():
  try:
    assets = json.loads(assets_path.read_text(encoding="utf-8"))
    if isinstance(assets, dict):
      payload["assets"] = assets
  except Exception:
    pass

pathlib.Path(${RESULTS_JSON@Q}).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY
}

if [[ -z "$python_path" ]]; then
  write_results "failure" "missing_report" 1
  exit 1
fi

if [[ ! -x "$python_path" ]]; then
  python_warning="python_path is not executable: $python_path"
fi

# Keep all HF/transformers caches within benchmark_assets/cache.
export HF_HOME="$CACHE_DIR/huggingface"
export HF_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$CACHE_DIR/transformers"
export XDG_CACHE_HOME="$CACHE_DIR/xdg"

mkdir -p "$HF_HOME" "$HF_HUB_CACHE" "$TRANSFORMERS_CACHE" "$XDG_CACHE_HOME"

dataset_path="$DATASET_DIR/prompts.json"
dataset_cache_path="$CACHE_DIR/dataset/prompts.json"
model_local_dir="$MODEL_DIR/huggingface/$(basename "$model_id")"
model_manifest_path="$MODEL_DIR/model_manifest.json"

echo "[prepare] repo_root=$REPO_ROOT"
echo "[prepare] python_path=$python_path ($python_resolution)"
echo "[prepare] model_id=$model_id revision=${model_revision:-<default>}"
echo "[prepare] offline=$offline"
echo "[prepare] dataset_path=$dataset_path"
echo "[prepare] dataset_cache_path=$dataset_cache_path"
echo "[prepare] model_local_dir=$model_local_dir"

set +e
"$python_path" - <<PY
import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

repo_root = pathlib.Path(${REPO_ROOT@Q})
assets_dir = pathlib.Path(${ASSETS_DIR@Q})
dataset_path = pathlib.Path(${dataset_path@Q})
dataset_cache_path = pathlib.Path(${dataset_cache_path@Q})
model_local_dir = pathlib.Path(${model_local_dir@Q})
assets_json_path = pathlib.Path(${ASSETS_JSON@Q})
model_manifest_path = pathlib.Path(${model_manifest_path@Q})

model_id = ${model_id@Q}
model_revision = ${model_revision@Q} or None
offline = bool(int(${offline@Q}))

def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def write_prompts_dataset() -> dict:
    dataset_cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "prompts_v1",
        "prompts": [
            "introduce yourself",
        ],
        "source_note": "Derived from repo example.py prompts; minimized to 1 prompt for batch_size=1.",
    }
    # "Download" into cache first (for offline reuse), then copy into dataset/.
    dataset_cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    dataset_path.write_text(dataset_cache_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "path": str(dataset_path),
        "source": "repo:example.py",
        "version": "prompts_v1",
        "sha256": sha256_file(dataset_cache_path),
    }

def try_snapshot_download() -> tuple[dict, str]:
    try:
        from huggingface_hub import HfApi, snapshot_download
    except Exception as e:
        raise RuntimeError(f"huggingface_hub not available: {e}")

    model_local_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = os.environ.get("HF_HUB_CACHE") or None
    allow_patterns = None
    ignore_patterns = None

    # Download (or reuse local cache). If offline is True, force local_files_only.
    local_files_only = offline
    resolved_path = snapshot_download(
        repo_id=model_id,
        revision=model_revision,
        local_dir=str(model_local_dir),
        local_dir_use_symlinks=False,
        resume_download=True,
        cache_dir=cache_dir,
        allow_patterns=allow_patterns,
        ignore_patterns=ignore_patterns,
        local_files_only=local_files_only,
    )

    # Try to obtain stable metadata from the Hub (best-effort).
    version = ""
    siblings = []
    try:
        info = HfApi().model_info(model_id, revision=model_revision)
        version = getattr(info, "sha", "") or ""
        siblings = []
        for s in getattr(info, "siblings", []) or []:
            entry = {"rfilename": getattr(s, "rfilename", "")}
            lfs = getattr(s, "lfs", None)
            if lfs is not None:
                entry["lfs_oid"] = getattr(lfs, "oid", "")
                entry["size"] = getattr(lfs, "size", None)
            siblings.append(entry)
    except Exception:
        pass

    manifest = {
        "model_id": model_id,
        "requested_revision": model_revision or "",
        "resolved_revision": version,
        "resolved_path": str(resolved_path),
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": sorted(siblings, key=lambda x: x.get("rfilename", "")),
    }

    # A reproducible hash without re-hashing GB-scale weight files.
    manifest_bytes = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest["manifest_sha256"] = manifest_sha256

    model_manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    model_asset = {
        "path": str(model_local_dir),
        "source": f"hf:{model_id}",
        "version": version or (model_revision or ""),
        "sha256": manifest_sha256,
    }
    return model_asset, manifest_sha256

dataset_asset = write_prompts_dataset()

model_asset = None
model_sha = ""
download_error = ""

reuse_ok = model_local_dir.exists() and any(model_local_dir.glob("*.safetensors"))
manifest_ok = model_manifest_path.exists()
manifest_match = False
if manifest_ok:
    try:
        m0 = json.loads(model_manifest_path.read_text(encoding="utf-8"))
        manifest_match = (
            m0.get("model_id") == model_id
            and (m0.get("requested_revision") or "") == (model_revision or "")
            and isinstance(m0.get("manifest_sha256"), str)
        )
    except Exception:
        manifest_match = False

if reuse_ok and manifest_ok and manifest_match:
    m = json.loads(model_manifest_path.read_text(encoding="utf-8"))
    model_asset = {
        "path": str(model_local_dir),
        "source": f"hf:{model_id}",
        "version": m.get("resolved_revision", "") or m.get("requested_revision", ""),
        "sha256": m.get("manifest_sha256", ""),
    }
else:
    try:
        model_asset, model_sha = try_snapshot_download()
    except Exception as e:
        download_error = f"{type(e).__name__}: {e}"
        if reuse_ok:
            model_asset = {
                "path": str(model_local_dir),
                "source": f"hf:{model_id}",
                "version": model_revision or "",
                "sha256": "",
            }
        else:
            raise

assets = {"dataset": dataset_asset, "model": model_asset}
assets_dir.mkdir(parents=True, exist_ok=True)
assets_json_path.write_text(json.dumps(assets, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

print(json.dumps({"ok": True, "download_error": download_error, "assets": assets}, ensure_ascii=False))
PY
prepare_py_rc=$?
set -e

if [[ "$prepare_py_rc" -ne 0 ]]; then
  failure_category="download_failed"
  if grep -Ei '401|403|unauthorized|forbidden|token' "$LOG_TXT" >/dev/null 2>&1; then
    failure_category="auth_required"
  elif grep -Ei 'No module named|huggingface_hub not available|pip' "$LOG_TXT" >/dev/null 2>&1; then
    failure_category="deps"
  elif grep -Ei 'temporary failure|name or service not known|connection|timed out|readtimeout|proxy' "$LOG_TXT" >/dev/null 2>&1; then
    failure_category="download_failed"
  fi
  write_results "failure" "$failure_category" 1
  exit 1
fi

write_results "success" "" 0
exit 0
