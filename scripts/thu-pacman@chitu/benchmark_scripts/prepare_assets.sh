#!/usr/bin/env bash
set -uo pipefail

# Downloads a minimal dataset + model into benchmark_assets/, and writes build_output/prepare/results.json.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 1

out_dir="$repo_root/build_output/prepare"
mkdir -p "$out_dir"
log_file="$out_dir/log.txt"
results_json="$out_dir/results.json"

exec > >(tee "$log_file") 2>&1

report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_exec="${SCIMLOPSBENCH_PYTHON:-}"

if [[ -z "$python_exec" ]]; then
  if [[ -f "$report_path" ]]; then
    python_exec="$(jq -r '.python_path // empty' "$report_path" 2>/dev/null || true)"
  fi
fi

if [[ -z "$python_exec" ]]; then
  cat >"$results_json" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {"SCIMLOPSBENCH_REPORT": "",
                "SCIMLOPSBENCH_PYTHON": ""},
    "decision_reason": "No SCIMLOPSBENCH_PYTHON and report.json missing/invalid."
  },
  "failure_category": "missing_report",
  "error_excerpt": "Missing report.json or python_path; set SCIMLOPSBENCH_PYTHON or provide a valid /opt/scimlopsbench/report.json."
}
JSON
  exit 1
fi

echo "[prepare] Using python: $python_exec"

export BENCHMARK_ASSETS_DIR="$repo_root/benchmark_assets"
export BENCHMARK_CACHE_DIR="$repo_root/benchmark_assets/cache"
export BENCHMARK_DATASET_DIR="$repo_root/benchmark_assets/dataset"
export BENCHMARK_MODEL_DIR="$repo_root/benchmark_assets/model"

mkdir -p "$BENCHMARK_CACHE_DIR" "$BENCHMARK_DATASET_DIR" "$BENCHMARK_MODEL_DIR"

# Keep all caches inside benchmark_assets/cache
export HF_HOME="$BENCHMARK_CACHE_DIR/huggingface"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export TORCH_HOME="$BENCHMARK_CACHE_DIR/torch"
export XDG_CACHE_HOME="$BENCHMARK_CACHE_DIR/xdg"

# Defaults (override via env vars)
export SCIMLOPSBENCH_MODEL_NAME="${SCIMLOPSBENCH_MODEL_NAME:-Qwen2.5-0.5B}"
export SCIMLOPSBENCH_MODEL_REPO_ID="${SCIMLOPSBENCH_MODEL_REPO_ID:-Qwen/Qwen2.5-0.5B}"
export SCIMLOPSBENCH_MODEL_REVISION="${SCIMLOPSBENCH_MODEL_REVISION:-}"

export SCIMLOPSBENCH_DATASET_SOURCE_URL="${SCIMLOPSBENCH_DATASET_SOURCE_URL:-https://raw.githubusercontent.com/karpathy/char-rnn/master/data/tinyshakespeare/input.txt}"
export SCIMLOPSBENCH_DATASET_NAME="${SCIMLOPSBENCH_DATASET_NAME:-tinyshakespeare_sharegpt}"
export SCIMLOPSBENCH_DATASET_NUM_SAMPLES="${SCIMLOPSBENCH_DATASET_NUM_SAMPLES:-64}"

set +e
"$python_exec" - <<'PY'
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from typing import Optional

# This script is executed via stdin, so __file__ is not reliable. Use cwd (repo root)
# because prepare_assets.sh already `cd`'d into the repository root.
repo_root = pathlib.Path(".").resolve()
out_dir = repo_root / "build_output" / "prepare"
out_dir.mkdir(parents=True, exist_ok=True)
results_path = out_dir / "results.json"
log_path = out_dir / "log.txt"

cache_dir = pathlib.Path(os.environ["BENCHMARK_CACHE_DIR"])
dataset_dir = pathlib.Path(os.environ["BENCHMARK_DATASET_DIR"])
model_dir = pathlib.Path(os.environ["BENCHMARK_MODEL_DIR"])

model_name = os.environ.get("SCIMLOPSBENCH_MODEL_NAME", "").strip()
model_repo_id = os.environ.get("SCIMLOPSBENCH_MODEL_REPO_ID", "").strip()
model_revision = os.environ.get("SCIMLOPSBENCH_MODEL_REVISION", "").strip() or None

dataset_url = os.environ.get("SCIMLOPSBENCH_DATASET_SOURCE_URL", "").strip()
dataset_name = os.environ.get("SCIMLOPSBENCH_DATASET_NAME", "").strip() or "dataset"
try:
    dataset_num_samples = int(os.environ.get("SCIMLOPSBENCH_DATASET_NUM_SAMPLES", "64"))
except Exception:
    dataset_num_samples = 64

def tail_text(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_size = min(size, 512 * 1024)
            if read_size > 0:
                f.seek(-read_size, os.SEEK_END)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-max_lines:])
    except Exception:
        return ""

def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def ensure_dir(p: pathlib.Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def is_dir_nonempty(p: pathlib.Path) -> bool:
    try:
        return p.is_dir() and any(p.iterdir())
    except Exception:
        return False

def safe_remove(p: pathlib.Path) -> None:
    try:
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)
    except Exception:
        pass

def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT).strip()
    except Exception:
        return ""

assets = {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
}

status = "failure"
exit_code = 1
failure_category = "unknown"
command = ""
decision_reason = ""

try:
    ensure_dir(cache_dir)
    ensure_dir(dataset_dir)
    ensure_dir(model_dir)

    # -------- Dataset: download a small public text file, then convert to ShareGPT-like JSON --------
    ds_cache_dir = cache_dir / "dataset"
    ensure_dir(ds_cache_dir)
    raw_txt = ds_cache_dir / f"{dataset_name}.txt"
    prepared_json = dataset_dir / f"{dataset_name}.json"

    if not prepared_json.exists():
        if not raw_txt.exists():
            print(f"[prepare] Downloading dataset source: {dataset_url}")
            command = f"download {dataset_url} -> {raw_txt}"
            try:
                with urllib.request.urlopen(dataset_url, timeout=30) as resp:
                    data = resp.read()
                raw_txt.write_bytes(data)
            except urllib.error.URLError as e:
                failure_category = "download_failed"
                raise RuntimeError(f"dataset download failed: {e}")
        else:
            print(f"[prepare] Reusing cached dataset source: {raw_txt}")

        text = raw_txt.read_text(encoding="utf-8", errors="replace")
        chunks: list[str] = []
        buf: list[str] = []
        buf_len = 0
        # Build reasonably long prompts so ShareGPTDataset.sample can satisfy small input_len.
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            buf.append(line)
            buf_len += len(line) + 1
            if buf_len >= 400:
                chunks.append(" ".join(buf))
                buf = []
                buf_len = 0
            if len(chunks) >= max(dataset_num_samples, 8):
                break
        if not chunks:
            failure_category = "data"
            raise RuntimeError("dataset conversion produced 0 samples")

        samples = chunks[:dataset_num_samples]
        payload = []
        for s in samples:
            payload.append(
                {
                    "conversations": [
                        {"from": "human", "value": s},
                        {"from": "gpt", "value": ""},
                    ]
                }
            )
        prepared_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assets["dataset"]["path"] = str(prepared_json.resolve())
    assets["dataset"]["source"] = dataset_url
    assets["dataset"]["version"] = "n/a"
    assets["dataset"]["sha256"] = sha256_file(prepared_json)

    # -------- Model: HuggingFace snapshot download into cache; link into benchmark_assets/model --------
    decision_reason = (
        "Chitu offline benchmark entrypoint requires a local checkpoint+tokenizer. "
        "Using the smallest built-in Chitu model config (Qwen2.5-0.5B) by default."
    )

    if not model_repo_id or not model_name:
        failure_category = "args_unknown"
        raise RuntimeError("SCIMLOPSBENCH_MODEL_NAME/SCIMLOPSBENCH_MODEL_REPO_ID must be set")

    # Install huggingface_hub if needed (best effort).
    try:
        import huggingface_hub  # type: ignore
    except Exception:
        print("[prepare] huggingface_hub not found; attempting to install via pip...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"], timeout=600)
        except Exception as e:
            # Offline reuse is still possible if model is already present.
            print(f"[prepare] pip install huggingface_hub failed: {e}")

    cache_models_root = cache_dir / "hf_models"
    ensure_dir(cache_models_root)

    safe_id = model_repo_id.replace("/", "__")
    model_cache_target = cache_models_root / safe_id
    model_link = model_dir / safe_id

    def has_model_artifacts(p: pathlib.Path) -> bool:
        if not is_dir_nonempty(p):
            return False
        # Try to find any weight file and tokenizer
        weight_ok = any(p.rglob("*.safetensors")) or any(p.rglob("pytorch_model*.bin"))
        tok_ok = (p / "tokenizer.json").exists() or (p / "tokenizer.model").exists() or (p / "tokenizer_config.json").exists()
        return weight_ok and tok_ok

    resolved_model_path: Optional[pathlib.Path] = None
    if has_model_artifacts(model_cache_target):
        resolved_model_path = model_cache_target
        print(f"[prepare] Reusing cached model: {resolved_model_path}")
    else:
        try:
            from huggingface_hub import snapshot_download  # type: ignore
        except Exception as e:
            failure_category = "deps"
            raise RuntimeError(f"huggingface_hub unavailable and no cached model found at {model_cache_target}: {e}")

        print(f"[prepare] Downloading model snapshot: {model_repo_id} (revision={model_revision or 'default'})")
        command = f"snapshot_download {model_repo_id} -> {model_cache_target}"
        try:
            safe_remove(model_cache_target)
            resolved = snapshot_download(
                repo_id=model_repo_id,
                revision=model_revision,
                local_dir=str(model_cache_target),
                local_dir_use_symlinks=False,
                resume_download=True,
            )
            resolved_model_path = pathlib.Path(resolved)
        except Exception as e:
            msg = str(e)
            if "401" in msg or "403" in msg or "gated" in msg.lower() or "token" in msg.lower():
                failure_category = "auth_required"
            else:
                failure_category = "download_failed"
            raise

        if not resolved_model_path.exists():
            failure_category = "model"
            raise RuntimeError(f"download reported success but resolved model dir not found: {resolved_model_path}")
        if not has_model_artifacts(resolved_model_path):
            failure_category = "model"
            raise RuntimeError(
                f"downloaded model dir does not appear complete (no weights/tokenizer detected): {resolved_model_path}"
            )

    # Link (or copy) into benchmark_assets/model
    try:
        if model_link.exists() or model_link.is_symlink():
            safe_remove(model_link)
        model_link.symlink_to(resolved_model_path, target_is_directory=True)
        final_model_path = resolved_model_path.resolve()
    except Exception:
        # Fall back to a shallow copy of the directory metadata is too expensive; copy tree if small only.
        # For large models, keep using cache path as the canonical path.
        final_model_path = resolved_model_path.resolve()

    # Directory-manifest sha256 (avoid hashing multi-GB weights again on every run)
    manifest = {
        "repo_id": model_repo_id,
        "revision": model_revision or "",
        "files": [],
    }
    try:
        for p in sorted(final_model_path.rglob("*")):
            if p.is_file():
                rel = p.relative_to(final_model_path).as_posix()
                try:
                    sz = p.stat().st_size
                except Exception:
                    sz = None
                manifest["files"].append({"path": rel, "size": sz})
    except Exception:
        pass
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    model_manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    assets["model"]["path"] = str(final_model_path)
    assets["model"]["source"] = f"https://huggingface.co/{model_repo_id}"
    assets["model"]["version"] = model_revision or "default"
    assets["model"]["sha256"] = model_manifest_sha

    status = "success"
    exit_code = 0
    failure_category = "unknown"

except Exception as e:
    if failure_category == "unknown":
        failure_category = "unknown"
    status = "failure"
    exit_code = 1
    print(f"[prepare] ERROR: {e}")

results = {
    "status": status,
    "skip_reason": "unknown",
    "exit_code": exit_code,
    "stage": "prepare",
    "task": "download",
    "command": command,
    "timeout_sec": 1200,
    "framework": "unknown",
    "assets": assets,
    "meta": {
        "python": sys.executable,
        "git_commit": git_commit(),
        "env_vars": {
            "HF_HOME": os.environ.get("HF_HOME", ""),
            "TRANSFORMERS_CACHE": os.environ.get("TRANSFORMERS_CACHE", ""),
            "HUGGINGFACE_HUB_CACHE": os.environ.get("HUGGINGFACE_HUB_CACHE", ""),
            "TORCH_HOME": os.environ.get("TORCH_HOME", ""),
        },
        "decision_reason": decision_reason,
        "model_name": model_name,
        "model_repo_id": model_repo_id,
        "dataset_name": dataset_name,
    },
    "failure_category": failure_category,
    "error_excerpt": tail_text(log_path),
}

results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
sys.exit(exit_code)
PY
rc=$?
set -e

exit $rc
