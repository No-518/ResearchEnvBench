#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model weights) under benchmark_assets/.

Defaults:
  - Uses python_path from agent report.json (SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  - Downloads public, anonymous Hugging Face artifacts:
      - SWivid/F5-TTS (F5TTS_v1_Base/model_1250000.safetensors)
      - charactr/vocos-mel-24khz (config.yaml, pytorch_model.bin)
  - Uses repo-included example audio as the minimal dataset input.

Optional:
  --python <path>          Explicit python executable to use (highest priority)
  --report-path <path>     Agent report.json path override
  --timeout-sec <int>      Default: 1200

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/
EOF
}

python_bin=""
report_path=""
timeout_sec="1200"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

out_dir="build_output/prepare"
mkdir -p "$out_dir"
log_path="$out_dir/log.txt"
results_json="$out_dir/results.json"

exec > >(tee "$log_path") 2>&1

BOOTSTRAP_PY="$(command -v python >/dev/null 2>&1 && echo python || echo python3)"
timestamp_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
git_commit="$(git rev-parse HEAD 2>/dev/null || true)"

status="success"
exit_code=0
failure_category="unknown"
skip_reason="unknown"
error_msg=""

PYTHON_EXE=""
REPORT_PATH_USED=""
python_source="unknown"

if [[ -n "$python_bin" ]]; then
  PYTHON_EXE="$python_bin"
  python_source="cli"
else
  rp_args=()
  [[ -n "$report_path" ]] && rp_args+=(--report-path "$report_path")
  resolved="$("$BOOTSTRAP_PY" benchmark_scripts/runner.py resolve-python --require-report "${rp_args[@]}" || true)"
  PYTHON_EXE="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("python",""))' <<<"$resolved" 2>/dev/null || true)"
  REPORT_PATH_USED="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("report_path",""))' <<<"$resolved" 2>/dev/null || true)"
  err="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("error",""))' <<<"$resolved" 2>/dev/null || true)"
  if [[ -z "$PYTHON_EXE" || -n "$err" ]]; then
    status="failure"
    exit_code=1
    failure_category="missing_report"
    error_msg="python resolution failed: ${err:-missing_report} (report_path=${REPORT_PATH_USED:-})"
  else
    python_source="report"
  fi
fi

if [[ "$status" == "success" ]]; then
  if ! "$PYTHON_EXE" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
    status="failure"
    exit_code=1
    failure_category="path_hallucination"
    error_msg="Resolved python is not executable: $PYTHON_EXE"
  fi
fi

cache_root="benchmark_assets/cache"
dataset_root="benchmark_assets/dataset"
model_root="benchmark_assets/model"

mkdir -p "$cache_root" "$dataset_root" "$model_root"
mkdir -p "$cache_root/huggingface" "$cache_root/xdg" "$cache_root/torch" "$cache_root/wandb"

export XDG_CACHE_HOME="$repo_root/$cache_root/xdg"
export HF_HOME="$repo_root/$cache_root/huggingface"
export HF_HUB_CACHE="$repo_root/$cache_root/huggingface/hub"
export HUGGINGFACE_HUB_CACHE="$repo_root/$cache_root/huggingface/hub"
export HF_DATASETS_CACHE="$repo_root/$cache_root/huggingface/datasets"
export TRANSFORMERS_CACHE="$repo_root/$cache_root/huggingface/transformers"
export TORCH_HOME="$repo_root/$cache_root/torch"
export WANDB_DIR="$repo_root/$cache_root/wandb"
export WANDB_MODE="${WANDB_MODE:-offline}"
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"

DATASET_REF_AUDIO_SRC="src/f5_tts/infer/examples/basic/basic_ref_en.wav"
DATASET_REF_TEXT="Some call me nature, others call me mother nature."
DATASET_GEN_TEXT="Here we generate something just for test."

DATASET_INFER_DIR="$dataset_root/infer_minimal"
DATASET_INPUT_DIR="$dataset_root/csv_wavs_minimal"
DATASET_PREP_DIR="$dataset_root/f5tts_minimal_pinyin"
DATASET_NAME_FOR_TRAIN="../benchmark_assets/dataset/f5tts_minimal"

MODEL_TTS_ID="SWivid/F5-TTS"
MODEL_TTS_FILE="F5TTS_v1_Base/model_1250000.safetensors"
MODEL_VOCODER_ID="charactr/vocos-mel-24khz"
MODEL_REVISION="main"

MODEL_TTS_DIR="$model_root/F5TTS_v1_Base"
MODEL_VOCODER_DIR="$model_root/vocos-mel-24khz"

DATASET_SHA256=""
MODEL_SHA256=""
MODEL_MANIFEST_JSON="{}"
TTS_CHECKPOINT_PATH=""

if [[ "$status" == "success" ]]; then
  mkdir -p "$DATASET_INFER_DIR" "$DATASET_INPUT_DIR/wavs" "$DATASET_PREP_DIR" "$MODEL_TTS_DIR" "$MODEL_VOCODER_DIR"
  if [[ ! -f "$DATASET_REF_AUDIO_SRC" ]]; then
    status="failure"
    exit_code=1
    failure_category="data"
    error_msg="Missing repo example audio: $DATASET_REF_AUDIO_SRC"
  fi
fi

if [[ "$status" == "success" ]]; then
  cp -f "$DATASET_REF_AUDIO_SRC" "$DATASET_INFER_DIR/ref_audio.wav"
  printf "%s\n" "$DATASET_REF_TEXT" > "$DATASET_INFER_DIR/ref_text.txt"
  printf "%s\n" "$DATASET_GEN_TEXT" > "$DATASET_INFER_DIR/gen_text.txt"

  printf "audio_path|text\nwavs/sample.wav|%s\n" "$DATASET_REF_TEXT" > "$DATASET_INPUT_DIR/metadata.csv"
  cp -f "$DATASET_INFER_DIR/ref_audio.wav" "$DATASET_INPUT_DIR/wavs/sample.wav"

  if [[ ! -s "$DATASET_PREP_DIR/raw.arrow" || ! -s "$DATASET_PREP_DIR/duration.json" || ! -s "$DATASET_PREP_DIR/vocab.txt" ]]; then
    echo "[prepare] Building minimal training dataset at $DATASET_PREP_DIR (ffprobe-free)..."
    dataset_build_json_path="$out_dir/dataset_build.json"
    rm -f "$dataset_build_json_path" 2>/dev/null || true
    set +e
    REPO_ROOT="$repo_root" \
    DATASET_WAV_REL="$DATASET_INPUT_DIR/wavs/sample.wav" \
    DATASET_PREP_DIR_REL="$DATASET_PREP_DIR" \
    DATASET_REF_TEXT="$DATASET_REF_TEXT" \
    DATASET_NUM_SAMPLES="2" \
    DATASET_BUILD_JSON_PATH="$dataset_build_json_path" \
      "$PYTHON_EXE" -u - <<'PY'
import json
import os
import shutil
import traceback
from pathlib import Path

def fail(cat: str, msg: str):
    out_json = Path(os.environ.get("DATASET_BUILD_JSON_PATH", "dataset_build.json"))
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps({"status": "failure", "failure_category": cat, "error": msg}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    raise SystemExit(2)

repo_root = Path(os.environ.get("REPO_ROOT", ".")).resolve()
input_wav = repo_root / os.environ["DATASET_WAV_REL"]
out_dir = repo_root / os.environ["DATASET_PREP_DIR_REL"]
ref_text = os.environ.get("DATASET_REF_TEXT", "")
num_samples = int(os.environ.get("DATASET_NUM_SAMPLES", "2") or "2")
out_json = Path(os.environ.get("DATASET_BUILD_JSON_PATH", "dataset_build.json"))

try:
    import torchaudio
except Exception as e:
    fail("deps", f"torchaudio_import_failed:{type(e).__name__}:{e}")

try:
    from datasets.arrow_writer import ArrowWriter
except Exception as e:
    fail("deps", f"datasets_import_failed:{type(e).__name__}:{e}")

try:
    from f5_tts.model.utils import convert_char_to_pinyin
    from importlib.resources import files
except Exception as e:
    fail("deps", f"f5_tts_import_failed:{type(e).__name__}:{e}")

if not input_wav.exists():
    fail("data", f"missing_audio:{input_wav}")

out_dir.mkdir(parents=True, exist_ok=True)

try:
    wav, sr = torchaudio.load(str(input_wav))
    if sr <= 0:
        raise ValueError(f"invalid_sample_rate:{sr}")
    duration = float(wav.shape[-1]) / float(sr)
except Exception as e:
    fail("deps", f"torchaudio_load_failed:{type(e).__name__}:{e}")

try:
    tokens = convert_char_to_pinyin([ref_text], polyphone=True)[0]
    if not isinstance(tokens, list):
        raise TypeError(f"expected_list_tokens_got:{type(tokens).__name__}")
except Exception as e:
    fail("deps", f"convert_char_to_pinyin_failed:{type(e).__name__}:{e}")

audio_path_str = os.environ["DATASET_WAV_REL"]
rows = [{"audio_path": audio_path_str, "text": tokens, "duration": duration} for _ in range(max(1, num_samples))]

raw_arrow_path = out_dir / "raw.arrow"
try:
    with ArrowWriter(path=str(raw_arrow_path)) as writer:
        for row in rows:
            writer.write(row)
        writer.finalize()
except Exception as e:
    fail("data", f"raw_arrow_write_failed:{type(e).__name__}:{e}")

dur_json_path = out_dir / "duration.json"
try:
    dur_json_path.write_text(
        json.dumps({"duration": [duration for _ in range(len(rows))]}, ensure_ascii=False),
        encoding="utf-8",
    )
except Exception as e:
    fail("data", f"duration_json_write_failed:{type(e).__name__}:{e}")

vocab_out = out_dir / "vocab.txt"
try:
    pretrained_vocab = files("f5_tts").joinpath("../../data/Emilia_ZH_EN_pinyin/vocab.txt")
    if not pretrained_vocab.exists():
        fail("data", f"missing_pretrained_vocab:{pretrained_vocab}")
    shutil.copy2(str(pretrained_vocab), str(vocab_out))
except SystemExit:
    raise
except Exception as e:
    fail("data", f"vocab_copy_failed:{type(e).__name__}:{e}")

out_json.parent.mkdir(parents=True, exist_ok=True)
out_json.write_text(
    json.dumps(
        {
            "status": "success",
            "prepared_dir": str(out_dir),
            "audio_path": audio_path_str,
            "duration_sec": duration,
            "raw_arrow": str(raw_arrow_path),
            "vocab": str(vocab_out),
        },
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
PY
    prep_rc=$?
    set -e

    dataset_ok=0
    if [[ -s "$DATASET_PREP_DIR/raw.arrow" && -s "$DATASET_PREP_DIR/duration.json" && -s "$DATASET_PREP_DIR/vocab.txt" ]]; then
      dataset_ok=1
    fi

    if [[ $dataset_ok -eq 1 ]]; then
      echo "[prepare] dataset_prep_ok: $DATASET_PREP_DIR"
    else
      prep_fc="data"
      prep_err="dataset_prep_failed"
      if [[ -s "$dataset_build_json_path" ]]; then
        prep_fc="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.load(open(sys.argv[1],"r",encoding="utf-8")).get("failure_category","data"))' "$dataset_build_json_path" 2>/dev/null || echo data)"
        prep_err="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.load(open(sys.argv[1],"r",encoding="utf-8")).get("error","dataset_prep_failed"))' "$dataset_build_json_path" 2>/dev/null || echo dataset_prep_failed)"
      fi
      echo "[prepare] dataset_prep_failed: rc=$prep_rc $prep_fc: $prep_err"
      status="failure"
      exit_code=1
      failure_category="$prep_fc"
      error_msg="$prep_err"
    fi
  else
    echo "[prepare] Dataset already prepared at $DATASET_PREP_DIR (cache hit)"
  fi
fi

MODEL_DOWNLOAD_JSON=""
if [[ "$PYTHON_EXE" != "" ]]; then
  set +e
  MODEL_DOWNLOAD_JSON="$("$PYTHON_EXE" -u - <<PY
import hashlib
import json
import os
import shutil
from pathlib import Path

repo_root = Path(${repo_root@Q})
cache_dir = repo_root / ${cache_root@Q} / "huggingface" / "hub"

tts_repo = ${MODEL_TTS_ID@Q}
tts_file = ${MODEL_TTS_FILE@Q}
vocoder_repo = ${MODEL_VOCODER_ID@Q}
vocoder_files = ["config.yaml", "pytorch_model.bin"]
revision = ${MODEL_REVISION@Q}

tts_dir = repo_root / ${MODEL_TTS_DIR@Q}
vocoder_dir = repo_root / ${MODEL_VOCODER_DIR@Q}

for d in [cache_dir, tts_dir, vocoder_dir]:
    d.mkdir(parents=True, exist_ok=True)

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def place_file(src: Path, dst: Path) -> None:
    # Hugging Face cache snapshot files are often symlinks with relative targets (../../blobs/...).
    # Never hardlink the symlink itself into benchmark_assets/model (it would become a broken link).
    src_real = src
    try:
        src_real = src.resolve(strict=True)
    except Exception:
        src_real = src

    if not src_real.exists():
        raise FileNotFoundError(f"downloaded_file_missing:{src}")

    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src_real, dst)
        if dst.exists():
            return
    except Exception:
        pass
    try:
        dst.symlink_to(src_real)
        if dst.exists():
            return
    except Exception:
        pass
    shutil.copy2(src_real, dst)

try:
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import HfHubHTTPError
except Exception as e:
    print(json.dumps({"status": "failure", "failure_category": "deps", "error": f"huggingface_hub_import_failed:{type(e).__name__}:{e}"}))
    raise SystemExit(3)

def download_file(repo_id: str, filename: str):
    try:
        p = hf_hub_download(repo_id=repo_id, filename=filename, cache_dir=str(cache_dir), revision=revision, local_files_only=True)
        return Path(p), "cache_hit", None
    except Exception:
        try:
            p = hf_hub_download(repo_id=repo_id, filename=filename, cache_dir=str(cache_dir), revision=revision, local_files_only=False)
            return Path(p), "downloaded", None
        except HfHubHTTPError as e2:
            code = getattr(getattr(e2, "response", None), "status_code", None)
            if code in (401, 403):
                return None, None, {"failure_category": "auth_required", "error": f"auth_required:{code}:{e2}"}
            return None, None, {"failure_category": "download_failed", "error": f"hf_http_error:{code}:{e2}"}
        except Exception as e2:
            return None, None, {"failure_category": "download_failed", "error": f"download_failed:{type(e2).__name__}:{e2}"}

items = []

p_tts, mode, err = download_file(tts_repo, tts_file)
if err:
    print(json.dumps({"status": "failure", **err}))
    raise SystemExit(4)

tts_dst = tts_dir / Path(tts_file).name
place_file(p_tts, tts_dst)
if not tts_dst.exists():
    print(json.dumps({"status": "failure", "failure_category": "model", "error": f"placed_file_missing:{tts_dst}"}))
    raise SystemExit(4)
items.append({
    "repo_id": tts_repo,
    "filename": tts_file,
    "revision": revision,
    "cache_path": str(p_tts),
    "mode": mode,
    "placed_path": str(tts_dst),
    "sha256": sha256_file(tts_dst),
})

for vf in vocoder_files:
    p_v, mode, err = download_file(vocoder_repo, vf)
    if err:
        print(json.dumps({"status": "failure", **err}))
        raise SystemExit(5)
    v_dst = vocoder_dir / Path(vf).name
    place_file(p_v, v_dst)
    if not v_dst.exists():
        print(json.dumps({"status": "failure", "failure_category": "model", "error": f"placed_file_missing:{v_dst}"}))
        raise SystemExit(5)
    items.append({
        "repo_id": vocoder_repo,
        "filename": vf,
        "revision": revision,
        "cache_path": str(p_v),
        "mode": mode,
        "placed_path": str(v_dst),
        "sha256": sha256_file(v_dst),
    })

manifest = {"items": items}
model_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode("utf-8")).hexdigest()
print(json.dumps({
    "status": "success",
    "tts_dir": str(tts_dir),
    "vocoder_dir": str(vocoder_dir),
    "manifest": manifest,
    "model_sha256": model_hash,
}))
PY
)"
  dl_rc=$?
  set -e

  dl_status="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("status","failure"))' <<<"$MODEL_DOWNLOAD_JSON" 2>/dev/null || echo failure)"
  if [[ "$dl_status" != "success" ]]; then
    model_fc="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("failure_category","download_failed"))' <<<"$MODEL_DOWNLOAD_JSON" 2>/dev/null || echo download_failed)"
    model_err="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("error","model_download_failed"))' <<<"$MODEL_DOWNLOAD_JSON" 2>/dev/null || echo model_download_failed)"
    echo "[prepare] model_download_failed: rc=$dl_rc status=$dl_status $model_fc: $model_err"
    [[ -n "${MODEL_DOWNLOAD_JSON:-}" ]] && echo "[prepare] model_download_output: $MODEL_DOWNLOAD_JSON"
    status="failure"
    exit_code=1
    failure_category="$model_fc"
    if [[ -n "$error_msg" ]]; then
      error_msg="${error_msg}; model_download_failed:${model_fc}:${model_err}"
    else
      error_msg="model_download_failed:${model_fc}:${model_err}"
    fi
  else
    echo "[prepare] model_download_ok: $MODEL_DOWNLOAD_JSON"
  fi
fi

set +e
DATASET_SHA256="$("$BOOTSTRAP_PY" - <<PY
import hashlib, json
from pathlib import Path

repo_root = Path(${repo_root@Q})
paths = [
    repo_root / ${DATASET_INFER_DIR@Q} / "ref_audio.wav",
    repo_root / ${DATASET_INFER_DIR@Q} / "ref_text.txt",
    repo_root / ${DATASET_INFER_DIR@Q} / "gen_text.txt",
    repo_root / ${DATASET_PREP_DIR@Q} / "raw.arrow",
    repo_root / ${DATASET_PREP_DIR@Q} / "duration.json",
    repo_root / ${DATASET_PREP_DIR@Q} / "vocab.txt",
]
items = []
for p in paths:
    if not p.exists():
        continue
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    items.append({"path": str(p.relative_to(repo_root)), "sha256": h.hexdigest(), "size_bytes": p.stat().st_size})

manifest = {"items": sorted(items, key=lambda x: x["path"])}
print(hashlib.sha256(json.dumps(manifest, sort_keys=True).encode("utf-8")).hexdigest())
PY
)"
ds_hash_rc=$?
set -e
if [[ $ds_hash_rc -ne 0 ]]; then
  echo "[prepare] WARNING: failed to compute dataset sha256 (rc=$ds_hash_rc)"
  DATASET_SHA256=""
fi

if [[ -n "$MODEL_DOWNLOAD_JSON" ]]; then
  MODEL_SHA256="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("model_sha256",""))' <<<"$MODEL_DOWNLOAD_JSON" 2>/dev/null || true)"
  MODEL_MANIFEST_JSON="$("$BOOTSTRAP_PY" -c 'import json,sys; print(json.dumps(json.loads(sys.stdin.read()).get("manifest",{})))' <<<"$MODEL_DOWNLOAD_JSON" 2>/dev/null || echo "{}")"
  TTS_CHECKPOINT_PATH="$("$BOOTSTRAP_PY" -c 'import json,sys; d=json.loads(sys.stdin.read()); items=d.get("manifest",{}).get("items",[]); print(next((it.get("placed_path","") for it in items if isinstance(it,dict) and str(it.get("filename","")).endswith(".safetensors")), ""))' <<<"$MODEL_DOWNLOAD_JSON" 2>/dev/null || true)"
fi

export BENCH_PREP_REPO_ROOT="$repo_root"
export BENCH_PREP_RESULTS_JSON="$results_json"
export BENCH_PREP_LOG_PATH="$log_path"
export BENCH_PREP_STATUS="$status"
export BENCH_PREP_SKIP_REASON="$skip_reason"
export BENCH_PREP_EXIT_CODE="$exit_code"
export BENCH_PREP_TIMEOUT_SEC="$timeout_sec"
export BENCH_PREP_GIT_COMMIT="$git_commit"
export BENCH_PREP_TIMESTAMP_UTC="$timestamp_utc"
export BENCH_PREP_PYTHON_EXE="$PYTHON_EXE"
export BENCH_PREP_PYTHON_SOURCE="$python_source"
export BENCH_PREP_REPORT_PATH_USED="$REPORT_PATH_USED"

export BENCH_PREP_DATASET_ROOT="$repo_root/$dataset_root"
export BENCH_PREP_DATASET_SOURCE="repo://$DATASET_REF_AUDIO_SRC"
export BENCH_PREP_DATASET_VERSION="$git_commit"
export BENCH_PREP_DATASET_SHA256="$DATASET_SHA256"

export BENCH_PREP_MODEL_ROOT="$repo_root/$model_root"
export BENCH_PREP_MODEL_SOURCE="hf://SWivid/F5-TTS + hf://charactr/vocos-mel-24khz"
export BENCH_PREP_MODEL_VERSION="$MODEL_REVISION"
export BENCH_PREP_MODEL_SHA256="$MODEL_SHA256"
export BENCH_PREP_MODEL_MANIFEST_JSON="$MODEL_MANIFEST_JSON"
export BENCH_PREP_TTS_CHECKPOINT_PATH="$TTS_CHECKPOINT_PATH"

export BENCH_PREP_INFER_DIR="$repo_root/$DATASET_INFER_DIR"
export BENCH_PREP_TRAIN_INPUT_DIR="$repo_root/$DATASET_INPUT_DIR"
export BENCH_PREP_TRAIN_PREP_DIR="$repo_root/$DATASET_PREP_DIR"
export BENCH_PREP_TRAIN_DATASET_NAME_ARG="$DATASET_NAME_FOR_TRAIN"

export BENCH_PREP_ENV_HF_HOME="$HF_HOME"
export BENCH_PREP_ENV_HF_HUB_CACHE="$HF_HUB_CACHE"
export BENCH_PREP_ENV_HUGGINGFACE_HUB_CACHE="$HUGGINGFACE_HUB_CACHE"
export BENCH_PREP_ENV_TRANSFORMERS_CACHE="$TRANSFORMERS_CACHE"
export BENCH_PREP_ENV_HF_DATASETS_CACHE="$HF_DATASETS_CACHE"
export BENCH_PREP_ENV_XDG_CACHE_HOME="$XDG_CACHE_HOME"
export BENCH_PREP_ENV_TORCH_HOME="$TORCH_HOME"
export BENCH_PREP_ENV_PYTHONPATH="$PYTHONPATH"

export BENCH_PREP_FAILURE_CATEGORY="$failure_category"
export BENCH_PREP_ERROR_MSG="$error_msg"

set +e
"$BOOTSTRAP_PY" - <<'PY'
import json
import os
from pathlib import Path

def tail_text(path: Path, max_lines: int = 220, max_bytes: int = 256_000) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes), os.SEEK_SET)
            except Exception:
                pass
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception as e:
        return f"(failed to tail log: {type(e).__name__}: {e})"

repo_root = Path(os.environ["BENCH_PREP_REPO_ROOT"]).resolve()
out_path = Path(os.environ["BENCH_PREP_RESULTS_JSON"])
log_path = Path(os.environ["BENCH_PREP_LOG_PATH"])

status = os.environ.get("BENCH_PREP_STATUS", "failure")
exit_code = int(os.environ.get("BENCH_PREP_EXIT_CODE", "1") or "1")
failure_category = os.environ.get("BENCH_PREP_FAILURE_CATEGORY", "unknown") or "unknown"
error_msg = os.environ.get("BENCH_PREP_ERROR_MSG", "")

if status not in {"success", "failure", "skipped"}:
    status = "failure"
if status == "success" and exit_code != 0:
    status = "failure"
if status == "failure":
    exit_code = 1

model_manifest_json = os.environ.get("BENCH_PREP_MODEL_MANIFEST_JSON", "{}") or "{}"
try:
    model_manifest = json.loads(model_manifest_json)
    if not isinstance(model_manifest, dict):
        model_manifest = {}
except Exception:
    model_manifest = {}

payload = {
    "status": status,
    "skip_reason": os.environ.get("BENCH_PREP_SKIP_REASON", "unknown"),
    "exit_code": exit_code,
    "stage": "prepare",
    "task": "download",
    "command": "bash benchmark_scripts/prepare_assets.sh",
    "timeout_sec": int(os.environ.get("BENCH_PREP_TIMEOUT_SEC", "1200")),
    "framework": "pytorch",
    "assets": {
        "dataset": {
            "path": os.environ.get("BENCH_PREP_DATASET_ROOT", ""),
            "source": os.environ.get("BENCH_PREP_DATASET_SOURCE", ""),
            "version": os.environ.get("BENCH_PREP_DATASET_VERSION", ""),
            "sha256": os.environ.get("BENCH_PREP_DATASET_SHA256", ""),
        },
        "model": {
            "path": os.environ.get("BENCH_PREP_MODEL_ROOT", ""),
            "source": os.environ.get("BENCH_PREP_MODEL_SOURCE", ""),
            "version": os.environ.get("BENCH_PREP_MODEL_VERSION", ""),
            "sha256": os.environ.get("BENCH_PREP_MODEL_SHA256", ""),
        },
    },
    "meta": {
        "python": os.environ.get("BENCH_PREP_PYTHON_EXE", ""),
        "python_source": os.environ.get("BENCH_PREP_PYTHON_SOURCE", "unknown"),
        "git_commit": os.environ.get("BENCH_PREP_GIT_COMMIT", ""),
        "env_vars": {
            "HF_HOME": os.environ.get("BENCH_PREP_ENV_HF_HOME", ""),
            "HF_HUB_CACHE": os.environ.get("BENCH_PREP_ENV_HF_HUB_CACHE", ""),
            "HUGGINGFACE_HUB_CACHE": os.environ.get("BENCH_PREP_ENV_HUGGINGFACE_HUB_CACHE", ""),
            "TRANSFORMERS_CACHE": os.environ.get("BENCH_PREP_ENV_TRANSFORMERS_CACHE", ""),
            "HF_DATASETS_CACHE": os.environ.get("BENCH_PREP_ENV_HF_DATASETS_CACHE", ""),
            "XDG_CACHE_HOME": os.environ.get("BENCH_PREP_ENV_XDG_CACHE_HOME", ""),
            "TORCH_HOME": os.environ.get("BENCH_PREP_ENV_TORCH_HOME", ""),
            "PYTHONPATH": os.environ.get("BENCH_PREP_ENV_PYTHONPATH", ""),
            "SCIMLOPSBENCH_REPORT": os.environ.get("BENCH_PREP_REPORT_PATH_USED", ""),
        },
        "decision_reason": (
            "Use repo example audio for minimal dataset; create a 1-sample preprocessed dataset without ffprobe; "
            "download public HF model + vocoder weights into benchmark_assets/cache and link into benchmark_assets/model."
        ),
        "dataset": {
            "infer_dir": os.environ.get("BENCH_PREP_INFER_DIR", ""),
            "train_input_dir": os.environ.get("BENCH_PREP_TRAIN_INPUT_DIR", ""),
            "train_prepared_dir": os.environ.get("BENCH_PREP_TRAIN_PREP_DIR", ""),
            "train_dataset_name_arg": os.environ.get("BENCH_PREP_TRAIN_DATASET_NAME_ARG", ""),
        },
        "model_download": model_manifest,
        "tts_checkpoint_path": os.environ.get("BENCH_PREP_TTS_CHECKPOINT_PATH", ""),
        "timestamp_utc": os.environ.get("BENCH_PREP_TIMESTAMP_UTC", ""),
        "notes": [error_msg] if error_msg else [],
    },
    "failure_category": failure_category,
    "error_excerpt": tail_text(log_path),
}

out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
write_rc=$?
set -e

if [[ $write_rc -ne 0 ]]; then
  echo "[prepare] ERROR: failed to write $results_json (rc=$write_rc)" >&2
  exit 1
fi

exit "$exit_code"
