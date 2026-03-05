#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model) into benchmark_assets/, with cache in benchmark_assets/cache/.

Defaults:
  - Uses report.json python_path unless overridden by --python or SCIMLOPSBENCH_PYTHON.
  - Dataset: Hugging Face dataset "alephpi/UniMER-Train" (tiny subset)
  - Model:   Hugging Face model  "alephpi/FormulaNet"

Optional:
  --python <path>            Override python executable
  --report-path <path>       Override report.json path (default: /opt/scimlopsbench/report.json)
  --dataset-id <id>          Override dataset id (default: alephpi/UniMER-Train)
  --model-id <id>            Override model id (default: alephpi/FormulaNet)
  --dataset-samples <n>      Number of samples to save (default: 1)
EOF
}

python_override=""
report_path=""
dataset_id="alephpi/UniMER-Train"
model_id="alephpi/FormulaNet"
dataset_samples="1"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_override="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --dataset-id) dataset_id="${2:-}"; shift 2 ;;
    --model-id) model_id="${2:-}"; shift 2 ;;
    --dataset-samples) dataset_samples="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

OUT_DIR="${REPO_ROOT}/build_output/prepare"
ASSETS_ROOT="${REPO_ROOT}/benchmark_assets"
CACHE_ROOT="${ASSETS_ROOT}/cache"
DATASET_OUT="${ASSETS_ROOT}/dataset"
MODEL_OUT="${ASSETS_ROOT}/model"

mkdir -p "$OUT_DIR" "$CACHE_ROOT" "$DATASET_OUT" "$MODEL_OUT"

LOG_PATH="${OUT_DIR}/log.txt"
RESULTS_PATH="${OUT_DIR}/results.json"
exec >"$LOG_PATH" 2>&1

echo "[prepare] repo_root=$REPO_ROOT"
echo "[prepare] dataset_id=$dataset_id dataset_samples=$dataset_samples"
echo "[prepare] model_id=$model_id"

REPORT_PATH="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"

resolve_python() {
  local json_py
  json_py="$(command -v python3 || true)"
  [[ -z "$json_py" ]] && json_py="$(command -v python || true)"
  [[ -n "$python_override" ]] && { echo "$python_override"; return 0; }
  [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]] && { echo "$SCIMLOPSBENCH_PYTHON"; return 0; }
  [[ -n "$json_py" ]] || { echo ""; return 0; }
  "$json_py" - <<PY 2>/dev/null || true
import json, pathlib
rp = pathlib.Path("$REPORT_PATH")
try:
    data = json.loads(rp.read_text(encoding="utf-8"))
    print(data.get("python_path","") or "")
except Exception:
    print("")
PY
}

PYTHON_BIN="$(resolve_python)"
json_py="$(command -v python3 || true)"
if [[ -z "$json_py" ]]; then
  json_py="$(command -v python || true)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "[prepare] ERROR: failed to resolve python (report missing/invalid and no override)"
  if [[ -n "$json_py" ]]; then
    RESULTS_PATH="$RESULTS_PATH" "$json_py" - <<'PY' || true
import json
import os
from datetime import datetime, timezone
from pathlib import Path

payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "python resolution failed",
    "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
  },
  "failure_category": "missing_report",
  "error_excerpt": "python resolution failed",
}
Path(os.environ["RESULTS_PATH"]).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  else
    cat >"$RESULTS_PATH" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "python resolution failed",
    "timestamp_utc": ""
  },
  "failure_category": "missing_report",
  "error_excerpt": "python resolution failed"
}
JSON
  fi
  exit 1
fi

echo "[prepare] python=$PYTHON_BIN"
if ! "$PYTHON_BIN" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  echo "[prepare] ERROR: resolved python is not runnable: $PYTHON_BIN"
  if [[ -n "$json_py" ]]; then
    PYTHON_BIN="$PYTHON_BIN" RESULTS_PATH="$RESULTS_PATH" "$json_py" - <<'PY' || true
import json
import os
from datetime import datetime, timezone
from pathlib import Path

payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": os.environ.get("PYTHON_BIN",""),
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "python probe failed",
    "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
  },
  "failure_category": "path_hallucination",
  "error_excerpt": "python probe failed",
}
Path(os.environ["RESULTS_PATH"]).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  else
    cat >"$RESULTS_PATH" <<JSON
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "${PYTHON_BIN}",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "python probe failed",
    "timestamp_utc": ""
  },
  "failure_category": "path_hallucination",
  "error_excerpt": "python probe failed"
}
JSON
  fi
  exit 1
fi

export HF_HOME="${CACHE_ROOT}/hf"
export HF_DATASETS_CACHE="${HF_HOME}/datasets"
export HF_HUB_CACHE="${HF_HOME}/hub"
export TRANSFORMERS_CACHE="${HF_HOME}/transformers"
export XDG_CACHE_HOME="${CACHE_ROOT}/xdg"
export TOKENIZERS_PARALLELISM="false"

DATASET_CACHE_DIR="${CACHE_ROOT}/dataset/${dataset_id//\//__}/samples=${dataset_samples}"
MODEL_CACHE_DIR="${CACHE_ROOT}/model/${model_id//\//__}"

DATASET_LINK_DIR="${DATASET_OUT}/UniMER-Train"
EVAL_LINK_DIR="${DATASET_OUT}/UniMER-Eval"
MODEL_LINK_DIR="${MODEL_OUT}/FormulaNet"

REPO_ROOT="$REPO_ROOT" OUT_DIR="$OUT_DIR" RESULTS_PATH="$RESULTS_PATH" LOG_PATH="$LOG_PATH" \
PYTHON_BIN="$PYTHON_BIN" REPORT_PATH="$REPORT_PATH" \
DATASET_ID="$dataset_id" DATASET_SAMPLES="$dataset_samples" MODEL_ID="$model_id" \
DATASET_CACHE_DIR="$DATASET_CACHE_DIR" MODEL_CACHE_DIR="$MODEL_CACHE_DIR" \
DATASET_LINK_DIR="$DATASET_LINK_DIR" EVAL_LINK_DIR="$EVAL_LINK_DIR" MODEL_LINK_DIR="$MODEL_LINK_DIR" \
"$PYTHON_BIN" - <<'PY'
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

repo_root = Path(os.environ["REPO_ROOT"])
out_dir = Path(os.environ["OUT_DIR"])
results_path = Path(os.environ["RESULTS_PATH"])
log_path = Path(os.environ["LOG_PATH"])

python_bin = os.environ["PYTHON_BIN"]
report_path = os.environ["REPORT_PATH"]
dataset_id = os.environ["DATASET_ID"]
dataset_samples = int(os.environ.get("DATASET_SAMPLES", "1"))
model_id = os.environ["MODEL_ID"]

dataset_cache_dir = Path(os.environ["DATASET_CACHE_DIR"])
model_cache_dir = Path(os.environ["MODEL_CACHE_DIR"])
dataset_link_dir = Path(os.environ["DATASET_LINK_DIR"])
eval_link_dir = Path(os.environ["EVAL_LINK_DIR"])
model_link_dir = Path(os.environ["MODEL_LINK_DIR"])

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def git_commit() -> str:
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], cwd=str(repo_root), text=True, stderr=subprocess.STDOUT, timeout=10).strip()
    except Exception:
        return ""

def dir_sha256(path: Path) -> str:
    if not path.exists():
        return ""
    if path.is_file():
        h = sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    files = sorted([p for p in path.rglob("*") if p.is_file() and not p.is_symlink()])
    h = sha256()
    for p in files:
        rel = p.relative_to(path).as_posix().encode("utf-8")
        h.update(rel)
        h.update(b"\0")
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()

def safe_symlink(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(src, target_is_directory=True)

def write_results(status: str, exit_code: int, failure_category: str, cmd: str, assets: dict, error_excerpt: str) -> None:
    payload = {
        "status": status,
        "skip_reason": "not_applicable" if status != "skipped" else "unknown",
        "exit_code": exit_code,
        "stage": "prepare",
        "task": "download",
        "command": cmd,
        "timeout_sec": 1200,
        "framework": "unknown",
        "assets": assets,
        "meta": {
            "python": python_bin,
            "git_commit": git_commit(),
            "env_vars": {
                "HF_HOME": os.environ.get("HF_HOME",""),
                "HF_DATASETS_CACHE": os.environ.get("HF_DATASETS_CACHE",""),
                "HF_HUB_CACHE": os.environ.get("HF_HUB_CACHE",""),
                "TRANSFORMERS_CACHE": os.environ.get("TRANSFORMERS_CACHE",""),
                "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME",""),
            },
            "decision_reason": "Use README-recommended HF dataset/model; save tiny dataset subset to disk for MERDatasetHF; keep downloads under benchmark_assets/cache.",
            "report_path": report_path,
            "timestamp_utc": utc_now_iso(),
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

assets = {
    "dataset": {"path": "", "source": f"hf://datasets/{dataset_id}", "version": f"samples={dataset_samples}", "sha256": ""},
    "model": {"path": "", "source": f"hf://models/{model_id}", "version": "main", "sha256": ""},
}

try:
    # Dataset: reuse cache if present.
    dataset_cache_dir.mkdir(parents=True, exist_ok=True)
    train_saved = dataset_cache_dir / "train"
    eval_saved = dataset_cache_dir / "eval"
    if train_saved.exists() and eval_saved.exists():
        print(f"[prepare] reuse dataset cache: {dataset_cache_dir}")
    else:
        print(f"[prepare] downloading dataset: {dataset_id}")
        from datasets import Dataset, Features, Value, load_dataset
        import io
        from PIL import Image

        def load_small() -> Dataset:
            try:
                return load_dataset(dataset_id, split=f"train[:{dataset_samples}]")
            except Exception:
                ds_full = load_dataset(dataset_id, split="train")
                return ds_full.select(range(min(dataset_samples, len(ds_full))))

        ds = load_small()
        samples = []
        for i in range(min(dataset_samples, len(ds))):
            item = ds[i]
            if "image" not in item or "text" not in item:
                raise RuntimeError(f"dataset item missing required keys: {item.keys()}")
            img = item["image"]
            if isinstance(img, (bytes, bytearray)):
                img_bytes = bytes(img)
            elif isinstance(img, dict) and isinstance(img.get("bytes", None), (bytes, bytearray)):
                img_bytes = bytes(img["bytes"])
            elif isinstance(img, Image.Image):
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                img_bytes = buf.getvalue()
            elif isinstance(img, dict) and isinstance(img.get("path", None), str):
                img_path = Path(img["path"])
                img_bytes = img_path.read_bytes()
            else:
                raise RuntimeError(f"unsupported image type from dataset: {type(img)}")
            txt = str(item["text"])
            if not txt:
                raise RuntimeError("empty text label in dataset sample")
            samples.append({"image": img_bytes, "text": txt})

        features = Features({"image": Value("binary"), "text": Value("string")})
        ds_small = Dataset.from_list(samples, features=features)

        train_saved.mkdir(parents=True, exist_ok=True)
        eval_saved.mkdir(parents=True, exist_ok=True)
        ds_small.save_to_disk(train_saved)
        ds_small.save_to_disk(eval_saved)
        print(f"[prepare] dataset saved: {train_saved} and {eval_saved}")

    # Model: snapshot_download into cache (reuse if present).
    model_cache_dir.mkdir(parents=True, exist_ok=True)
    model_root = model_cache_dir / "repo"
    if model_root.exists():
        print(f"[prepare] reuse model cache: {model_root}")
    else:
        print(f"[prepare] downloading model repo: {model_id}")
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=model_id, local_dir=model_root, local_dir_use_symlinks=False)

    # Validate model dir contains expected artifacts.
    expected = []
    for pat in ["config.json", "pytorch_model.bin", "*.safetensors", "checkpoints/*.pt", "tokenizer.json"]:
        expected.extend(list(model_root.rglob(pat)))
    if not expected:
        raise RuntimeError(f"model download finished but no expected artifacts found under {model_root} (searched for config.json/pytorch_model.bin/*.safetensors/checkpoints/*.pt)")

    # Link prepared assets.
    safe_symlink(train_saved, dataset_link_dir)
    safe_symlink(eval_saved, eval_link_dir)
    safe_symlink(model_root, model_link_dir)

    assets["dataset"]["path"] = str(dataset_link_dir)
    assets["model"]["path"] = str(model_link_dir)
    assets["dataset"]["sha256"] = dir_sha256(train_saved)
    assets["model"]["sha256"] = dir_sha256(model_root)

    write_results("success", 0, "unknown", "benchmark_scripts/prepare_assets.sh", assets, "")
except Exception as e:
    msg = f"{type(e).__name__}: {e}"
    print(f"[prepare] ERROR: {msg}")
    fc = "unknown"
    low = msg.lower()
    if "401" in low or "403" in low or "auth" in low or "token" in low:
        fc = "auth_required"
    elif "connection" in low or "temporary failure" in low or "name or service not known" in low or "ssl" in low:
        fc = "download_failed"
    elif "datasets" in low or "huggingface_hub" in low:
        fc = "deps"
    elif "dataset" in low:
        fc = "data"
    elif "model" in low:
        fc = "model"
    write_results("failure", 1, fc, "benchmark_scripts/prepare_assets.sh", assets, msg)
    raise SystemExit(1)
PY
