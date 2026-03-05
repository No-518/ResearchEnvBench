#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare minimal benchmark assets for Open-Sora:
  - Dataset: minimal prompt CSV (from repo assets)
  - Model: minimal checkpoints required by configs/diffusion/inference/256px.py

Writes:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Downloads/caches to:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Options:
  --report-path <path>   Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --python <path>        Override python interpreter used for downloads (else: report.json python_path)

Auth (optional, if needed for private assets):
  HF_TOKEN / HF_AUTH_TOKEN
EOF
}

report_path=""
python_bin=""

while [[ $# -gt 0 ]]; do
  case "$1" in
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_DIR="$REPO_ROOT/build_output/prepare"
ASSETS_ROOT="$REPO_ROOT/benchmark_assets"
CACHE_DIR="$ASSETS_ROOT/cache"
DATASET_DIR="$ASSETS_ROOT/dataset"
MODEL_DIR="$ASSETS_ROOT/model"

mkdir -p "$OUT_DIR" "$CACHE_DIR" "$DATASET_DIR" "$MODEL_DIR"

LOG_FILE="$OUT_DIR/log.txt"
RESULTS_JSON="$OUT_DIR/results.json"
ASSETS_INFO_JSON="$OUT_DIR/assets_info.json"

: >"$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[prepare] repo_root=$REPO_ROOT"
echo "[prepare] assets_root=$ASSETS_ROOT"

status="failure"
failure_category="unknown"
exit_code=1
skip_reason="unknown"
command="benchmark_scripts/prepare_assets.sh"
timeout_sec=1200
decision_reason="Using official Open-Sora inference config (configs/diffusion/inference/256px.py) to determine required checkpoints; using repo-provided prompt CSV assets/texts/example.csv as minimal dataset."

pybin_fallback() { command -v python3 >/dev/null 2>&1 && echo python3 || echo python; }

resolve_python_from_report() {
  local rp="$1"
  local pybin
  pybin="$(pybin_fallback)"
  "$pybin" - "$rp" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
data = json.loads(p.read_text(encoding="utf-8"))
print((data.get("python_path") or "").strip())
PY
}

rp="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"

if [[ -z "$python_bin" ]]; then
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    python_bin="${SCIMLOPSBENCH_PYTHON}"
  elif [[ -f "$rp" ]]; then
    python_bin="$(resolve_python_from_report "$rp" 2>/dev/null || true)"
  fi
fi

python_source="unknown"
if [[ -n "$python_bin" ]]; then
  python_source="selected"
else
  python_source="missing_report"
  echo "[prepare] ERROR: cannot resolve python (set --python or provide report.json with python_path)" >&2
  failure_category="missing_report"
fi

write_results() {
  local pybin
  pybin="$(pybin_fallback)"
  "$pybin" - "$RESULTS_JSON" "$ASSETS_INFO_JSON" "$LOG_FILE" "$rp" <<'PY'
import json
import os
import pathlib
import sys

results_path = pathlib.Path(sys.argv[1])
assets_info_path = pathlib.Path(sys.argv[2])
log_path = pathlib.Path(sys.argv[3])
report_path = sys.argv[4]

def tail(path: pathlib.Path, n: int = 220) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except Exception:
        return ""

assets_info = {}
try:
    assets_info = json.loads(assets_info_path.read_text(encoding="utf-8"))
except Exception:
    assets_info = {}

payload = {
    "status": os.environ.get("PREP_STATUS", "failure"),
    "skip_reason": os.environ.get("PREP_SKIP_REASON", "unknown"),
    "exit_code": int(os.environ.get("PREP_EXIT_CODE", "1")),
    "stage": "prepare",
    "task": "download",
    "command": os.environ.get("PREP_COMMAND", "benchmark_scripts/prepare_assets.sh"),
    "timeout_sec": int(os.environ.get("PREP_TIMEOUT_SEC", "1200")),
    "framework": "unknown",
    "assets": assets_info.get("assets", {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }),
    "meta": {
        "python": os.environ.get("PREP_PYTHON_EXE", ""),
        "python_version": os.environ.get("PREP_PYTHON_VER", ""),
        "git_commit": os.environ.get("PREP_GIT_COMMIT", ""),
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            "HF_HOME": os.environ.get("HF_HOME", ""),
            "HUGGINGFACE_HUB_CACHE": os.environ.get("HUGGINGFACE_HUB_CACHE", ""),
            "TRANSFORMERS_CACHE": os.environ.get("TRANSFORMERS_CACHE", ""),
            "HF_DATASETS_CACHE": os.environ.get("HF_DATASETS_CACHE", ""),
            "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", ""),
        },
        "decision_reason": os.environ.get("PREP_DECISION_REASON", ""),
        "report_path": report_path,
        "python_source": os.environ.get("PREP_PYTHON_SOURCE", ""),
        "deps": {
            "huggingface_hub_install_attempted": bool(int(os.environ.get("PREP_HF_INSTALL_ATTEMPTED", "0"))),
            "huggingface_hub_install_cmd": os.environ.get("PREP_HF_INSTALL_CMD", ""),
        },
        "downloads": assets_info.get("downloads", {}),
        "warnings": assets_info.get("warnings", []),
    },
    "failure_category": os.environ.get("PREP_FAILURE_CATEGORY", "unknown"),
    "error_excerpt": tail(log_path),
}

results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

trap 'write_results' EXIT

if [[ "$failure_category" == "missing_report" ]]; then
  export PREP_STATUS="failure" PREP_FAILURE_CATEGORY="missing_report" PREP_EXIT_CODE="1" PREP_SKIP_REASON="$skip_reason"
  export PREP_COMMAND="$command" PREP_TIMEOUT_SEC="$timeout_sec" PREP_DECISION_REASON="$decision_reason" PREP_PYTHON_SOURCE="$python_source"
  exit 1
fi

if [[ ! -x "$python_bin" ]]; then
  echo "[prepare] ERROR: python not executable: $python_bin" >&2
  export PREP_STATUS="failure" PREP_FAILURE_CATEGORY="path_hallucination" PREP_EXIT_CODE="1" PREP_SKIP_REASON="$skip_reason"
  export PREP_COMMAND="$command" PREP_TIMEOUT_SEC="$timeout_sec" PREP_DECISION_REASON="$decision_reason" PREP_PYTHON_SOURCE="$python_source"
  export PREP_PYTHON_EXE="$python_bin" PREP_PYTHON_VER=""
  exit 1
fi

python_exe="$("$python_bin" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
python_ver="$("$python_bin" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
export PREP_PYTHON_EXE="$python_exe" PREP_PYTHON_VER="$python_ver"
export PREP_GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
export PREP_DECISION_REASON="$decision_reason" PREP_COMMAND="$command" PREP_TIMEOUT_SEC="$timeout_sec" PREP_PYTHON_SOURCE="$python_source"

echo "[prepare] python_exe=$python_exe"
echo "[prepare] python_ver=$python_ver"

set +e
"$python_bin" -m pip --version >/dev/null 2>&1
pip_rc=$?
set -e
if [[ "$pip_rc" -ne 0 ]]; then
  echo "[prepare] ERROR: pip not available in selected python" >&2
  export PREP_STATUS="failure" PREP_FAILURE_CATEGORY="deps" PREP_EXIT_CODE="1"
  exit 1
fi

hf_install_attempted=0
hf_install_cmd=""
if ! "$python_bin" -c 'import huggingface_hub' >/dev/null 2>&1; then
  hf_install_attempted=1
  hf_install_cmd="$python_exe -m pip install -q huggingface_hub"
  echo "[prepare] installing huggingface_hub: $hf_install_cmd"
  set +e
  install_out="$("$python_bin" -m pip install -q huggingface_hub 2>&1)"
  install_rc=$?
  set -e
  if [[ "$install_rc" -ne 0 ]]; then
    echo "$install_out" >&2
    if echo "$install_out" | rg -i "(temporary failure|name resolution|connection|timed out|proxy|ssl)" >/dev/null 2>&1; then
      failure_category="download_failed"
    else
      failure_category="deps"
    fi
    export PREP_STATUS="failure" PREP_FAILURE_CATEGORY="$failure_category" PREP_EXIT_CODE="1"
    export PREP_HF_INSTALL_ATTEMPTED="$hf_install_attempted" PREP_HF_INSTALL_CMD="$hf_install_cmd"
    exit 1
  fi
fi
export PREP_HF_INSTALL_ATTEMPTED="$hf_install_attempted" PREP_HF_INSTALL_CMD="$hf_install_cmd"

# Direct common caches into benchmark_assets/cache
export HF_HOME="$CACHE_DIR/hf_home"
export HUGGINGFACE_HUB_CACHE="$CACHE_DIR/huggingface/hub"
export TRANSFORMERS_CACHE="$CACHE_DIR/huggingface/transformers"
export HF_DATASETS_CACHE="$CACHE_DIR/huggingface/datasets"
export XDG_CACHE_HOME="$CACHE_DIR/xdg"
mkdir -p "$HF_HOME" "$HUGGINGFACE_HUB_CACHE" "$TRANSFORMERS_CACHE" "$HF_DATASETS_CACHE" "$XDG_CACHE_HOME"

echo "[prepare] HF_HOME=$HF_HOME"
echo "[prepare] HUGGINGFACE_HUB_CACHE=$HUGGINGFACE_HUB_CACHE"

set +e
"$python_bin" - "$REPO_ROOT" "$ASSETS_ROOT" "$CACHE_DIR" "$DATASET_DIR" "$MODEL_DIR" "$ASSETS_INFO_JSON" <<'PY'
import csv
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
assets_root = Path(sys.argv[2]).resolve()
cache_dir = Path(sys.argv[3]).resolve()
dataset_dir = Path(sys.argv[4]).resolve()
model_dir = Path(sys.argv[5]).resolve()
assets_info_path = Path(sys.argv[6]).resolve()

info = {
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": str(model_dir), "source": "", "version": "", "sha256": "", "components": {}},
    },
    "downloads": {},
    "warnings": [],
    "failure_category": None,
}

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

def link_or_copy(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        return
    ensure_dir(dst.parent)
    try:
        dst.symlink_to(src)
    except Exception:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)

def find_weight_file(model_path: Path) -> Path | None:
    candidates = []
    for pat in ("*.safetensors", "pytorch_model*.bin", "*.bin"):
        candidates.extend(model_path.glob(pat))
    # pick largest
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        # search one level deep for sharded bins
        for p in model_path.rglob("pytorch_model*.bin"):
            if p.is_file():
                candidates.append(p)
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)

def load_manifest() -> dict | None:
    man = model_dir / "manifest.json"
    if not man.exists():
        return None
    try:
        return json.loads(man.read_text(encoding="utf-8"))
    except Exception:
        return None

def manifest_matches(man: dict) -> bool:
    try:
        comps = man.get("components", {})
        if not isinstance(comps, dict):
            return False
        for name, spec in comps.items():
            p = Path(spec.get("path", ""))
            expected = spec.get("sha256", "")
            if not p.exists():
                return False
            if expected and sha256_file(p) != expected:
                return False
        return True
    except Exception:
        return False

ensure_dir(cache_dir)
ensure_dir(dataset_dir)
ensure_dir(model_dir)

# -------------------------
# Dataset: minimal prompts
# -------------------------
src_csv = repo_root / "assets" / "texts" / "example.csv"
dst_csv = dataset_dir / "prompts.csv"
if not src_csv.exists():
    info["failure_category"] = "data"
    info["warnings"].append(f"Missing repo dataset seed: {src_csv}")
else:
    try:
        with src_csv.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            first = next(reader, None)
        ensure_dir(dst_csv.parent)
        with dst_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["text"])
            writer.writeheader()
            if first and "text" in first:
                writer.writerow({"text": first["text"]})
            else:
                writer.writerow({"text": "raining, sea"})
                info["warnings"].append("example.csv did not parse; wrote fallback prompt.")
        info["assets"]["dataset"] = {
            "path": str(dst_csv),
            "source": "repo:assets/texts/example.csv (first row)",
            "version": os.environ.get("PREP_GIT_COMMIT", ""),
            "sha256": sha256_file(dst_csv),
        }
    except Exception as e:
        info["failure_category"] = "data"
        info["warnings"].append(f"Failed to write dataset CSV: {e}")

# -------------------------
# Model: download/cache/link
# -------------------------
manifest = load_manifest()
if manifest and manifest_matches(manifest):
    info["downloads"]["skipped"] = True
else:
    try:
        from huggingface_hub import snapshot_download
    except Exception as e:
        info["failure_category"] = "deps"
        info["warnings"].append(f"huggingface_hub import failed: {e}")
    else:
        hf_root = cache_dir / "hf"
        ensure_dir(hf_root)

        def repo_dir(repo_id: str) -> Path:
            return hf_root / repo_id.replace("/", "__")

        downloads = []

        # Open-Sora weights (expected filenames per configs/diffusion/inference/256px.py)
        opensora_repo = "hpcai-tech/Open-Sora-v2"
        opensora_cache = repo_dir(opensora_repo)
        allow = ["Open_Sora_v2.safetensors", "hunyuan_vae.safetensors"]
        downloads.append(("opensora", opensora_repo, opensora_cache, allow))

        # T5 / CLIP (transformers)
        t5_repo = "google/t5-v1_1-xxl"
        t5_cache = repo_dir(t5_repo)
        t5_allow = ["*.json", "*.model", "*.txt", "*.safetensors", "pytorch_model*.bin"]
        downloads.append(("t5", t5_repo, t5_cache, t5_allow))

        clip_repo = "openai/clip-vit-large-patch14"
        clip_cache = repo_dir(clip_repo)
        clip_allow = ["*.json", "*.txt", "*.safetensors", "pytorch_model*.bin", "vocab.json", "merges.txt"]
        downloads.append(("clip", clip_repo, clip_cache, clip_allow))

        token = os.environ.get("HF_TOKEN") or os.environ.get("HF_AUTH_TOKEN") or None

        for name, repo_id, local_dir, patterns in downloads:
            if local_dir.exists():
                # best-effort: rely on snapshot_download to reuse local content
                pass
            ensure_dir(local_dir)
            try:
                resolved = snapshot_download(
                    repo_id=repo_id,
                    repo_type="model",
                    local_dir=str(local_dir),
                    local_dir_use_symlinks=False,
                    allow_patterns=patterns,
                    token=token,
                    resume_download=True,
                )
                info["downloads"][name] = {"repo_id": repo_id, "local_dir": resolved, "allow_patterns": patterns}
            except Exception as e:
                # Offline reuse: proceed if expected artifacts already exist
                info["warnings"].append(f"Download failed for {repo_id}: {e}")
                info["downloads"][name] = {"repo_id": repo_id, "local_dir": str(local_dir), "error": str(e)}

        # Resolve required artifacts from caches
        opensora_ckpt = opensora_cache / "Open_Sora_v2.safetensors"
        hunyuan_vae = opensora_cache / "hunyuan_vae.safetensors"
        t5_dir = t5_cache
        clip_dir = clip_cache

        if not opensora_ckpt.exists() or not hunyuan_vae.exists():
            info["failure_category"] = "model"
            info["warnings"].append(
                f"Expected Open-Sora weights not found under {opensora_cache} (need Open_Sora_v2.safetensors and hunyuan_vae.safetensors)."
            )
        else:
            # Link into model_dir
            link_or_copy(opensora_ckpt, model_dir / "Open_Sora_v2.safetensors")
            link_or_copy(hunyuan_vae, model_dir / "hunyuan_vae.safetensors")
            link_or_copy(t5_dir, model_dir / "google" / "t5-v1_1-xxl")
            link_or_copy(clip_dir, model_dir / "openai" / "clip-vit-large-patch14")

            # Verify and hash
            t5_weight = find_weight_file(model_dir / "google" / "t5-v1_1-xxl")
            clip_weight = find_weight_file(model_dir / "openai" / "clip-vit-large-patch14")
            if t5_weight is None:
                info["failure_category"] = "model"
                info["warnings"].append("Could not locate a weight file under t5 directory.")
            if clip_weight is None:
                info["failure_category"] = "model"
                info["warnings"].append("Could not locate a weight file under clip directory.")

            if info["failure_category"] is None:
                components = {
                    "opensora_ckpt": {
                        "path": str(model_dir / "Open_Sora_v2.safetensors"),
                        "source": opensora_repo,
                        "version": "main",
                        "sha256": sha256_file(model_dir / "Open_Sora_v2.safetensors"),
                    },
                    "hunyuan_vae": {
                        "path": str(model_dir / "hunyuan_vae.safetensors"),
                        "source": opensora_repo,
                        "version": "main",
                        "sha256": sha256_file(model_dir / "hunyuan_vae.safetensors"),
                    },
                    "t5": {
                        "path": str(model_dir / "google" / "t5-v1_1-xxl"),
                        "source": t5_repo,
                        "version": "main",
                        "sha256": sha256_file(t5_weight) if t5_weight else "",
                        "weight_file": str(t5_weight) if t5_weight else "",
                    },
                    "clip": {
                        "path": str(model_dir / "openai" / "clip-vit-large-patch14"),
                        "source": clip_repo,
                        "version": "main",
                        "sha256": sha256_file(clip_weight) if clip_weight else "",
                        "weight_file": str(clip_weight) if clip_weight else "",
                    },
                }
                info["assets"]["model"]["components"] = components

                manifest_obj = {"components": components}
                manifest_path = model_dir / "manifest.json"
                manifest_path.write_text(json.dumps(manifest_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                info["assets"]["model"]["sha256"] = sha256_file(manifest_path)
                info["assets"]["model"]["source"] = ";".join(
                    [opensora_repo, t5_repo, clip_repo]
                )
                info["assets"]["model"]["version"] = "main"

assets_info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
if info["failure_category"] is not None:
    sys.exit(1)
sys.exit(0)
PY
prep_rc=$?
set -e

if [[ "$prep_rc" -ne 0 ]]; then
  failure_category="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("failure_category","unknown"))' "$ASSETS_INFO_JSON" 2>/dev/null || echo "unknown")"
  export PREP_STATUS="failure" PREP_FAILURE_CATEGORY="$failure_category" PREP_EXIT_CODE="1"
  exit 1
fi

export PREP_STATUS="success" PREP_FAILURE_CATEGORY="unknown" PREP_EXIT_CODE="0" PREP_SKIP_REASON="not_applicable"
exit 0

