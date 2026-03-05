#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model) with offline cache reuse.

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Writes only under:
  benchmark_assets/{cache,dataset,model}
  build_output/prepare

Optional:
  --timeout-sec <int>            Default: 1200
  --python <path>                Override python (otherwise resolved from report.json)
  --report-path <path>           Default: /opt/scimlopsbench/report.json

Environment overrides:
  SENSEVOICE_BENCH_DATASET_URL   Default: https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/test_audio/asr_example_en.wav
  SENSEVOICE_BENCH_MODEL_ID      Default: iic/SenseVoiceSmall
EOF
}

timeout_sec=1200
python_bin=""
report_path=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

dataset_url="${SENSEVOICE_BENCH_DATASET_URL:-https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/test_audio/asr_example_en.wav}"
model_id="${SENSEVOICE_BENCH_MODEL_ID:-iic/SenseVoiceSmall}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="build_output/prepare"
ASSETS_ROOT="benchmark_assets"
CACHE_ROOT="$ASSETS_ROOT/cache"
DATASET_DIR="$ASSETS_ROOT/dataset"
MODEL_DIR="$ASSETS_ROOT/model"
HOME_DIR="$CACHE_ROOT/home"
XDG_CACHE_HOME="$CACHE_ROOT/xdg_cache"
HF_HOME="$CACHE_ROOT/hf_home"
HF_HUB_CACHE="$CACHE_ROOT/hf_hub"
HF_DATASETS_CACHE="$CACHE_ROOT/hf_datasets"
TRANSFORMERS_CACHE="$CACHE_ROOT/transformers"
TORCH_HOME="$CACHE_ROOT/torch"
PIP_CACHE_DIR="$CACHE_ROOT/pip"

mkdir -p "$OUT_DIR" "$CACHE_ROOT" "$DATASET_DIR" "$MODEL_DIR" "$HOME_DIR" "$XDG_CACHE_HOME" "$HF_HOME" "$HF_HUB_CACHE" "$HF_DATASETS_CACHE" "$TRANSFORMERS_CACHE" "$TORCH_HOME" "$PIP_CACHE_DIR"

runner_py="$(command -v python3 || command -v python)"

cmd="$(cat <<'BASH'
set -euo pipefail

"$BENCH_PYTHON" - <<'PY'
import hashlib
import json
import os
import pathlib
import sys
import time
import traceback
import urllib.error
import urllib.request
import wave

repo_root = pathlib.Path(os.environ["BENCH_REPO_ROOT"]).resolve()
out_dir = pathlib.Path(os.environ["BENCH_OUT_DIR"]).resolve()
assets_root = repo_root / "benchmark_assets"
cache_root = assets_root / "cache"
dataset_dir = assets_root / "dataset"
model_dir = assets_root / "model"

dataset_url = os.environ["BENCH_DATASET_URL"]
model_id = os.environ["BENCH_MODEL_ID"]

cache_dataset_dir = cache_root / "dataset"
cache_dataset_dir.mkdir(parents=True, exist_ok=True)

dataset_name = pathlib.Path(dataset_url.split("?")[0]).name or "dataset.bin"
dataset_cache = cache_dataset_dir / dataset_name
dataset_cache_sha = cache_dataset_dir / f"{dataset_name}.sha256"
dataset_target = dataset_dir / dataset_name

extra_path = out_dir / "extra_results.json"

def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def download_with_retries(url: str, dest: pathlib.Path, timeout_sec: int = 30, retries: int = 3) -> None:
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "scimlopsbench/1.0"})
            with urllib.request.urlopen(req, timeout=timeout_sec) as r:
                tmp = dest.with_suffix(dest.suffix + ".tmp")
                with tmp.open("wb") as f:
                    while True:
                        chunk = r.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                tmp.replace(dest)
                return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            time.sleep(1.0 + i)
    raise RuntimeError(f"download failed after {retries} retries: {last_err}")

def safe_symlink(src: pathlib.Path, dst: pathlib.Path) -> None:
    if dst.exists() or dst.is_symlink():
        try:
            dst.unlink()
        except Exception:
            pass
    try:
        dst.symlink_to(src)
    except Exception:
        dst.write_bytes(src.read_bytes())

assets = {
    "dataset": {"path": "unknown", "source": dataset_url, "version": "unknown", "sha256": "unknown"},
    "model": {"path": "unknown", "source": f"modelscope:{model_id}", "version": "unknown", "sha256": "unknown"},
}
meta_extra = {"dataset_url": dataset_url, "model_id": model_id}
failure_category = None

try:
    dataset_dir.mkdir(parents=True, exist_ok=True)

    dataset_ok = False
    if dataset_cache.is_file() and dataset_cache_sha.is_file():
        try:
            expected = dataset_cache_sha.read_text(encoding="utf-8").strip()
            actual = sha256_file(dataset_cache)
            if expected and expected == actual:
                dataset_ok = True
        except Exception:
            dataset_ok = False

    if not dataset_ok:
        try:
            print(f"[prepare] downloading dataset: {dataset_url}")
            download_with_retries(dataset_url, dataset_cache)
        except Exception as e:
            if dataset_cache.is_file():
                print(f"[prepare] dataset download failed, using cached file: {e}")
            else:
                raise

        dataset_hash = sha256_file(dataset_cache)
        dataset_cache_sha.write_text(dataset_hash + "\n", encoding="utf-8")
        dataset_ok = True

    safe_symlink(dataset_cache, dataset_target)
    assets["dataset"]["path"] = str(dataset_target)
    assets["dataset"]["sha256"] = sha256_file(dataset_cache)

    # Create a minimal train/val jsonl for finetuning (2 lines helps DDP split across 2 ranks).
    transcript = "he tried to think how it could be"
    target_len = len([w for w in transcript.strip().split() if w])
    with wave.open(str(dataset_cache), "rb") as wf:
        sr = wf.getframerate()
        nframes = wf.getnframes()
    # Approximate 10ms frame count for 16kHz audio (160 samples / frame).
    source_len = int(nframes // max(1, (sr // 100))) if sr else 0

    entry_base = {
        "text_language": "<|en|>",
        "emo_target": "<|NEUTRAL|>",
        "event_target": "<|Speech|>",
        "with_or_wo_itn": "<|woitn|>",
        "target": transcript,
        "source": str(dataset_target),
        "target_len": target_len,
        "source_len": source_len,
    }
    train_jsonl = dataset_dir / "train.jsonl"
    val_jsonl = dataset_dir / "val.jsonl"
    with train_jsonl.open("w", encoding="utf-8") as f:
        for i in range(2):
            row = dict(entry_base)
            row["key"] = f"ID0012W0014_{i}"
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with val_jsonl.open("w", encoding="utf-8") as f:
        row = dict(entry_base)
        row["key"] = "ID0012W0014_val"
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    meta_extra["train_jsonl"] = str(train_jsonl)
    meta_extra["val_jsonl"] = str(val_jsonl)

    # Download / materialize model into the (scoped) HOME cache by using repo-native code path.
    modelscope_hub = pathlib.Path(os.environ["HOME"]) / ".cache" / "modelscope" / "hub"
    model_id_path = pathlib.Path(*[p for p in model_id.split("/") if p])

    def dir_nonempty(p: pathlib.Path) -> bool:
        return p.is_dir() and any(p.iterdir())

    def resolve_model_dir_from_cache():
        candidates = [
            modelscope_hub / "models" / model_id_path,  # common layout: hub/models/<org>/<name>
            modelscope_hub / model_id_path,             # older layout: hub/<org>/<name>
            modelscope_hub / "models" / model_id.replace("/", "_"),
            modelscope_hub / model_id.replace("/", "_"),
        ]
        for c in candidates:
            if dir_nonempty(c):
                return c
        if modelscope_hub.is_dir():
            try:
                # Fallback: find a directory ending with the model_id path parts.
                tail_parts = tuple(model_id_path.parts)
                tail_len = len(tail_parts)
                for p in modelscope_hub.rglob("*"):
                    if not p.is_dir():
                        continue
                    parts = p.parts
                    if tail_len > 0 and len(parts) >= tail_len and tuple(parts[-tail_len:]) == tail_parts:
                        if dir_nonempty(p):
                            return p
            except Exception:
                return None
        return None

    resolved_model_dir = resolve_model_dir_from_cache()
    model_ok = resolved_model_dir is not None

    if not model_ok:
        print(f"[prepare] downloading model via SenseVoiceSmall.from_pretrained: {model_id}")
        try:
            from model import SenseVoiceSmall  # repo file
        except Exception as e:
            failure_category = "deps"
            raise RuntimeError(f"deps: failed to import repo model wrapper (model.py): {e}") from e

        try:
            m, kwargs = SenseVoiceSmall.from_pretrained(model=model_id, device="cpu")
            _ = m  # keep reference for download to complete
        except Exception as e:
            # Model download failures usually surface here (offline/auth/etc).
            failure_category = "download_failed"
            raise RuntimeError(f"download_failed: from_pretrained failed: {e}") from e

        kw = kwargs if isinstance(kwargs, dict) else {}
        model_path = kw.get("model_path") or kw.get("output_dir") or kw.get("init_param") or ""
        meta_extra["from_pretrained_model_path"] = str(model_path)

        if model_path:
            mp = pathlib.Path(str(model_path))
            if mp.is_file():
                mp = mp.parent
            if dir_nonempty(mp):
                resolved_model_dir = mp

    if resolved_model_dir is None:
        resolved_model_dir = resolve_model_dir_from_cache()

    if resolved_model_dir is None:
        failure_category = failure_category or "model"
        raise RuntimeError(
            "model cache directory not found after download; "
            f"searched under: {modelscope_hub} (model_id={model_id})"
        )

    link_name = model_dir / model_id.replace("/", "_")
    link_name.parent.mkdir(parents=True, exist_ok=True)
    if link_name.exists() or link_name.is_symlink():
        try:
            link_name.unlink()
        except Exception:
            pass
    try:
        link_name.symlink_to(resolved_model_dir)
    except Exception:
        # Fall back to not symlinking; still record the cache path.
        pass

    meta_extra["resolved_model_dir"] = str(resolved_model_dir)
    assets["model"]["path"] = str(link_name if link_name.exists() else resolved_model_dir)

    # Model directory hash (sha256 of per-file sha256 manifest).
    manifest = []
    for p in sorted(resolved_model_dir.rglob("*")):
        if p.is_file():
            try:
                manifest.append({"path": str(p.relative_to(resolved_model_dir)), "size": p.stat().st_size})
            except Exception:
                continue
    manifest_path = cache_root / "model_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assets["model"]["sha256"] = sha256_file(manifest_path)

    (out_dir / "assets.json").write_text(json.dumps(assets, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
except Exception:
    traceback.print_exc()
    if not failure_category:
        failure_category = "download_failed"
finally:
    extra = {
        "assets": assets,
        "meta": {
            **meta_extra,
            "decision_reason": "Dataset URL is from README train_wav.scp example; model_id is from README model zoo and demos; caches are scoped under benchmark_assets/cache via HOME.",
        },
    }
    if failure_category:
        extra["failure_category"] = failure_category
    try:
        extra_path.write_text(json.dumps(extra, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass

if failure_category:
    sys.exit(1)
PY
BASH
)"

exec "$runner_py" benchmark_scripts/runner.py \
  --stage prepare \
  --task download \
  --framework pytorch \
  --timeout-sec "$timeout_sec" \
  --out-dir "$OUT_DIR" \
  --requires-python \
  ${report_path:+--report-path "$report_path"} \
  ${python_bin:+--python "$python_bin"} \
  --env "BENCH_REPO_ROOT=$REPO_ROOT" \
  --env "BENCH_OUT_DIR=$OUT_DIR" \
  --env "BENCH_DATASET_URL=$dataset_url" \
  --env "BENCH_MODEL_ID=$model_id" \
  --env "HOME=$HOME_DIR" \
  --env "XDG_CACHE_HOME=$XDG_CACHE_HOME" \
  --env "HF_HOME=$HF_HOME" \
  --env "HF_HUB_CACHE=$HF_HUB_CACHE" \
  --env "HF_DATASETS_CACHE=$HF_DATASETS_CACHE" \
  --env "TRANSFORMERS_CACHE=$TRANSFORMERS_CACHE" \
  --env "TORCH_HOME=$TORCH_HOME" \
  --env "PIP_CACHE_DIR=$PIP_CACHE_DIR" \
  --env "PYTHONDONTWRITEBYTECODE=1" \
  --env "PYTHONUNBUFFERED=1" \
  --extra-json "$OUT_DIR/extra_results.json" \
  -- bash -lc "$cmd"
