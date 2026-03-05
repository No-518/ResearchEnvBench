#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Prepare minimal benchmark assets (dataset + model weights) under benchmark_assets/.

Outputs (always, even on failure):
  build_output/prepare/log.txt
  build_output/prepare/results.json

By default, this repo's only runnable entrypoint is `aim-v1/main_attnprobe.py`, which expects:
  - An ImageFolder-style dataset root containing `val/<class>/*.jpg`
  - A backbone checkpoint + attention head checkpoint for AIMv1

Optional overrides:
  --model <aim-600M|aim-1B|aim-3B|aim-7B>     Default: aim-600M
  --probe-layers <last|best>                  Default: last

Environment:
  SCIMLOPSBENCH_OFFLINE=1     If set, downloads are not attempted; cached assets must exist.
EOF
}

invocation=( "$0" "$@" )

model_name="aim-600M"
probe_layers="last"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      model_name="${2:-}"; shift 2 ;;
    --probe-layers)
      probe_layers="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_DIR="$ROOT/build_output/prepare"
ASSETS_DIR="$ROOT/benchmark_assets"
CACHE_DIR="$ASSETS_DIR/cache"
DATASET_DIR="$ASSETS_DIR/dataset"
MODEL_DIR="$ASSETS_DIR/model"
LOG_TXT="$STAGE_DIR/log.txt"
RESULTS_JSON="$STAGE_DIR/results.json"
MANIFEST_ENV="$ASSETS_DIR/manifest.env"

mkdir -p "$STAGE_DIR" "$CACHE_DIR/dataset" "$CACHE_DIR/model" "$DATASET_DIR" "$MODEL_DIR"

status="failure"
exit_code="0"
failure_category="unknown"
skip_reason="unknown"
decision_reason=""
command_str=""

dataset_root_rel=""
dataset_root_abs=""
dataset_source=""
dataset_version=""
dataset_sha256=""

model_root_rel=""
model_root_abs=""
model_source=""
model_version=""
model_sha256=""
backbone_ckpt_abs=""
head_ckpt_abs=""

python_exe="$(command -v python3 || true)"
git_commit="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
head_sanitized="0"
head_sanitized_removed_keys=""

quote_cmd() {
  local out=""
  for a in "$@"; do
    out+="${out:+ }"
    out+="$(printf '%q' "$a")"
  done
  printf '%s' "$out"
}

sha256_file() {
  local f="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$f" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$f" | awk '{print $1}'
  else
    python3 - "$f" <<'PY'
import hashlib
import sys
from pathlib import Path

p = Path(sys.argv[1])
h = hashlib.sha256()
with p.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
  fi
}

sha256_text() {
  local txt="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$txt" | sha256sum | awk '{print $1}'
  else
    python3 - "$txt" <<'PY'
import hashlib
import sys

h = hashlib.sha256(sys.argv[1].encode("utf-8"))
print(h.hexdigest())
PY
  fi
}

resolve_python_for_torch() {
  local cand=""

  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" && -x "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    echo "${SCIMLOPSBENCH_PYTHON}"
    return 0
  fi

  local report="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
  if [[ -f "$report" ]]; then
    cand="$(python3 - "$report" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)
py = data.get("python_path")
if isinstance(py, str) and py.strip():
    print(py.strip())
PY
)"
    if [[ -n "$cand" && -x "$cand" ]]; then
      echo "$cand"
      return 0
    fi
  fi

  if [[ -n "$python_exe" && -x "$python_exe" ]]; then
    echo "$python_exe"
    return 0
  fi

  return 1
}

sanitize_head_checkpoint_inplace() {
  local head_ckpt="$1"
  local py="$2"

  local tmp="${head_ckpt}.sanitized.tmp.$$"
  local out
  out="$("$py" - "$head_ckpt" "$tmp" <<'PY' 2>>"$LOG_TXT" || true
import json
import sys

src = sys.argv[1]
dst = sys.argv[2]

try:
    import torch
except Exception as e:
    print(json.dumps({"ok": False, "error": f"import_torch_failed: {e!r}"}))
    raise SystemExit(2)

obj = torch.load(src, map_location="cpu")
state_dict = None
container = None
if isinstance(obj, dict) and all(isinstance(k, str) for k in obj.keys()):
    # Either a raw state_dict (string->tensor) or a wrapped checkpoint.
    if "state_dict" in obj and isinstance(obj.get("state_dict"), dict):
        container = obj
        state_dict = obj["state_dict"]
    else:
        state_dict = obj
else:
    print(json.dumps({"ok": False, "error": "unsupported_checkpoint_format"}))
    raise SystemExit(3)

removed = []
for key in ("head.linear.bias", "module.head.linear.bias"):
    if key in state_dict:
        state_dict.pop(key, None)
        removed.append(key)

if container is not None:
    container["state_dict"] = state_dict
    torch.save(container, dst)
else:
    torch.save(state_dict, dst)

print(json.dumps({"ok": True, "removed_keys": removed, "dst": dst}))
PY
)"

  if [[ -z "$out" ]]; then
    echo "[prepare] head checkpoint sanitize: no output" >>"$LOG_TXT"
    rm -f "$tmp" 2>/dev/null || true
    return 1
  fi

  if python3 -c "import json,sys; d=json.loads(sys.argv[1]); assert d.get('ok') is True" "$out" >/dev/null 2>&1; then
    head_sanitized="1"
    head_sanitized_removed_keys="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(','.join(d.get('removed_keys') or []))" "$out" 2>/dev/null || true)"
    mv -f "$tmp" "$head_ckpt"
    echo "[prepare] head checkpoint sanitized in-place; removed_keys=${head_sanitized_removed_keys:-none}" >>"$LOG_TXT"
    return 0
  fi

  echo "[prepare] head checkpoint sanitize failed: $out" >>"$LOG_TXT"
  rm -f "$tmp" 2>/dev/null || true
  return 1
}

download_to() {
  local url="$1"
  local dst="$2"
  local tmp="$dst.tmp.$$"
  local offline="${SCIMLOPSBENCH_OFFLINE:-0}"

  if [[ "$offline" == "1" ]]; then
    echo "[prepare] offline mode set; skipping download for $url" >>"$LOG_TXT"
    return 3
  fi

  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --retry 3 --connect-timeout 10 --max-time 1200 -o "$tmp" "$url" >>"$LOG_TXT" 2>&1 || return $?
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$tmp" "$url" >>"$LOG_TXT" 2>&1 || return $?
  else
    echo "[prepare] neither curl nor wget is available" >>"$LOG_TXT"
    return 2
  fi

  mv -f "$tmp" "$dst"
  return 0
}

write_results() {
  local err_excerpt
  err_excerpt="$(tail -n 220 "$LOG_TXT" 2>/dev/null || true)"

  python3 - "$RESULTS_JSON" <<'PY'
import json
import os
import sys
from pathlib import Path

out = Path(sys.argv[1])

def _env_subset(keys):
    return {k: os.environ.get(k, "") for k in keys if os.environ.get(k) is not None}

payload = {
    "status": os.environ.get("STAGE_STATUS", "failure"),
    "skip_reason": os.environ.get("STAGE_SKIP_REASON", "unknown"),
    "exit_code": int(os.environ.get("STAGE_EXIT_CODE", "1")),
    "stage": "prepare",
    "task": "download",
    "command": os.environ.get("STAGE_COMMAND", ""),
    "timeout_sec": 1200,
    "framework": "unknown",
    "assets": {
        "dataset": {
            "path": os.environ.get("ASSET_DATASET_PATH", ""),
            "source": os.environ.get("ASSET_DATASET_SOURCE", ""),
            "version": os.environ.get("ASSET_DATASET_VERSION", ""),
            "sha256": os.environ.get("ASSET_DATASET_SHA256", ""),
        },
        "model": {
            "path": os.environ.get("ASSET_MODEL_PATH", ""),
            "source": os.environ.get("ASSET_MODEL_SOURCE", ""),
            "version": os.environ.get("ASSET_MODEL_VERSION", ""),
            "sha256": os.environ.get("ASSET_MODEL_SHA256", ""),
        },
    },
    "meta": {
        "python": os.environ.get("META_PYTHON", ""),
        "git_commit": os.environ.get("META_GIT_COMMIT", ""),
        "env_vars": _env_subset(
            [
                "SCIMLOPSBENCH_OFFLINE",
            ]
        ),
        "decision_reason": os.environ.get("META_DECISION_REASON", ""),
        "model_name": os.environ.get("META_MODEL_NAME", ""),
        "probe_layers": os.environ.get("META_PROBE_LAYERS", ""),
        "model_files": {
            "backbone_ckpt": os.environ.get("META_BACKBONE_CKPT", ""),
            "head_ckpt": os.environ.get("META_HEAD_CKPT", ""),
        },
        "head_checkpoint_sanitized": bool(int(os.environ.get("META_HEAD_SANITIZED", "0") or 0)),
        "head_checkpoint_sanitized_removed_keys": [
            s for s in (os.environ.get("META_HEAD_SANITIZED_KEYS", "") or "").split(",") if s
        ],
    },
    "failure_category": os.environ.get("STAGE_FAILURE_CATEGORY", "unknown"),
    "error_excerpt": os.environ.get("STAGE_ERROR_EXCERPT", ""),
}

out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

{
  echo "[prepare] root=$ROOT"
  echo "[prepare] model=$model_name probe_layers=$probe_layers"
  echo "[prepare] offline=${SCIMLOPSBENCH_OFFLINE:-0}"
} >"$LOG_TXT"

command_str="$(quote_cmd bash "${invocation[@]}")"

decision_reason="Using AIMv1 evaluation entrypoint (aim-v1/main_attnprobe.py) which requires an ImageFolder dataset root and explicit backbone/head checkpoints. ImageNet-1k is not anonymously downloadable, so prepare a minimal ImageFolder dataset from a single public image."

# --- Dataset: minimal ImageFolder (1 image) ---
dataset_name="imagefolder_1img"
dataset_root_rel="benchmark_assets/dataset/$dataset_name"
dataset_root_abs="$ROOT/$dataset_root_rel"
dataset_source="https://raw.githubusercontent.com/pytorch/hub/master/images/dog.jpg"
dataset_version="unknown"

cache_img="$CACHE_DIR/dataset/dog.jpg"
cache_img_sha="$cache_img.sha256"

echo "[prepare] dataset: downloading sample image to cache: $dataset_source" >>"$LOG_TXT"

cache_ok=0
if [[ -f "$cache_img" && -f "$cache_img_sha" ]]; then
  have_sha="$(cat "$cache_img_sha" 2>/dev/null || true)"
  cur_sha="$(sha256_file "$cache_img" 2>/dev/null || true)"
  if [[ -n "$have_sha" && "$have_sha" == "$cur_sha" ]]; then
    cache_ok=1
    echo "[prepare] dataset cache hit: $cache_img ($cur_sha)" >>"$LOG_TXT"
  fi
fi

if [[ "$cache_ok" -ne 1 ]]; then
  if download_to "$dataset_source" "$cache_img"; then
    dataset_sha256="$(sha256_file "$cache_img")"
    echo "$dataset_sha256" >"$cache_img_sha"
    echo "[prepare] dataset downloaded: sha256=$dataset_sha256" >>"$LOG_TXT"
  else
    dl_rc=$?
    echo "[prepare] dataset download failed (rc=$dl_rc)" >>"$LOG_TXT"
    if [[ -f "$cache_img" ]]; then
      dataset_sha256="$(sha256_file "$cache_img" 2>/dev/null || true)"
      if [[ -n "$dataset_sha256" ]]; then
        echo "[prepare] proceeding with existing cached dataset file (offline reuse): $cache_img" >>"$LOG_TXT"
        echo "$dataset_sha256" >"$cache_img_sha"
      else
        failure_category="download_failed"
        status="failure"
        exit_code="1"
      fi
    else
      failure_category="download_failed"
      status="failure"
      exit_code="1"
    fi
  fi
else
  dataset_sha256="$(cat "$cache_img_sha")"
fi

if [[ -z "${dataset_sha256:-}" ]]; then
  echo "[prepare] dataset sha256 unavailable; aborting" >>"$LOG_TXT"
  failure_category="download_failed"
  status="failure"
  exit_code="1"
else
  mkdir -p "$dataset_root_abs/val/class0"
  cp -f "$cache_img" "$dataset_root_abs/val/class0/dog.jpg"
  echo "[prepare] dataset prepared at: $dataset_root_abs" >>"$LOG_TXT"
fi

# --- Model: AIMv1 backbone + head checkpoints (download to cache; copy to model dir) ---
case "$model_name" in
  aim-600M)
    model_version="AIMv1"
    backbone_url="https://huggingface.co/apple/AIM/resolve/main/aim_600m_2bimgs_attnprobe_backbone.pth"
    if [[ "$probe_layers" == "best" ]]; then
      head_url="https://huggingface.co/apple/AIM/resolve/main/aim_600m_2bimgs_attnprobe_head_best_layers.pth"
    else
      head_url="https://huggingface.co/apple/AIM/resolve/main/aim_600m_2bimgs_attnprobe_head_last_layers.pth"
    fi
    ;;
  aim-1B)
    model_version="AIMv1"
    backbone_url="https://huggingface.co/apple/AIM/resolve/main/aim_1b_5bimgs_attnprobe_backbone.pth"
    if [[ "$probe_layers" == "best" ]]; then
      head_url="https://huggingface.co/apple/AIM/resolve/main/aim_1b_5bimgs_attnprobe_head_best_layers.pth"
    else
      head_url="https://huggingface.co/apple/AIM/resolve/main/aim_1b_5bimgs_attnprobe_head_last_layers.pth"
    fi
    ;;
  aim-3B)
    model_version="AIMv1"
    backbone_url="https://huggingface.co/apple/AIM/resolve/main/aim_3b_5bimgs_attnprobe_backbone.pth"
    if [[ "$probe_layers" == "best" ]]; then
      head_url="https://huggingface.co/apple/AIM/resolve/main/aim_3b_5bimgs_attnprobe_head_best_layers.pth"
    else
      head_url="https://huggingface.co/apple/AIM/resolve/main/aim_3b_5bimgs_attnprobe_head_last_layers.pth"
    fi
    ;;
  aim-7B)
    model_version="AIMv1"
    backbone_url="https://huggingface.co/apple/AIM/resolve/main/aim_7b_5bimgs_attnprobe_backbone.pth"
    if [[ "$probe_layers" == "best" ]]; then
      head_url="https://huggingface.co/apple/AIM/resolve/main/aim_7b_5bimgs_attnprobe_head_best_layers.pth"
    else
      head_url="https://huggingface.co/apple/AIM/resolve/main/aim_7b_5bimgs_attnprobe_head_last_layers.pth"
    fi
    ;;
  *)
    echo "[prepare] unknown --model: $model_name" >>"$LOG_TXT"
    failure_category="args_unknown"
    status="failure"
    exit_code="1"
    ;;
esac

model_source="HuggingFace: apple/AIM (direct resolve URLs)"
model_root_rel="benchmark_assets/model/aim-v1/${model_name}-${probe_layers}"
model_root_abs="$ROOT/$model_root_rel"

if [[ "$exit_code" -eq 1 && "$failure_category" == "args_unknown" ]]; then
  : # stop further work
else
  mkdir -p "$model_root_abs"

  cache_backbone="$CACHE_DIR/model/${model_name}_${probe_layers}_backbone.pth"
  cache_head="$CACHE_DIR/model/${model_name}_${probe_layers}_head.pth"
  cache_backbone_sha="$cache_backbone.sha256"
  cache_head_sha="$cache_head.sha256"

  echo "[prepare] model: downloading backbone to cache: $backbone_url" >>"$LOG_TXT"
  backbone_cache_ok=0
  if [[ -f "$cache_backbone" && -f "$cache_backbone_sha" ]]; then
    have_sha="$(cat "$cache_backbone_sha" 2>/dev/null || true)"
    cur_sha="$(sha256_file "$cache_backbone" 2>/dev/null || true)"
    if [[ -n "$have_sha" && "$have_sha" == "$cur_sha" ]]; then
      backbone_cache_ok=1
      echo "[prepare] backbone cache hit: $cache_backbone ($cur_sha)" >>"$LOG_TXT"
    fi
  fi
  if [[ "$backbone_cache_ok" -ne 1 ]]; then
    if [[ -f "$cache_backbone" ]]; then
      echo "[prepare] backbone cache present but sha mismatch/missing; re-downloading" >>"$LOG_TXT"
    fi
    if download_to "$backbone_url" "$cache_backbone"; then
      backbone_sha="$(sha256_file "$cache_backbone" 2>/dev/null || true)"
      if [[ -n "$backbone_sha" ]]; then
        echo "$backbone_sha" >"$cache_backbone_sha"
      fi
    else
      echo "[prepare] backbone download failed" >>"$LOG_TXT"
      if [[ -f "$cache_backbone" ]]; then
        backbone_sha="$(sha256_file "$cache_backbone" 2>/dev/null || true)"
        if [[ -n "$backbone_sha" ]]; then
          echo "[prepare] proceeding with existing cached backbone (offline reuse): $cache_backbone" >>"$LOG_TXT"
          echo "$backbone_sha" >"$cache_backbone_sha"
        else
          failure_category="download_failed"
          status="failure"
          exit_code="1"
        fi
      else
        failure_category="download_failed"
        status="failure"
        exit_code="1"
      fi
    fi
  fi

  echo "[prepare] model: downloading head to cache: $head_url" >>"$LOG_TXT"
  head_cache_ok=0
  if [[ -f "$cache_head" && -f "$cache_head_sha" ]]; then
    have_sha="$(cat "$cache_head_sha" 2>/dev/null || true)"
    cur_sha="$(sha256_file "$cache_head" 2>/dev/null || true)"
    if [[ -n "$have_sha" && "$have_sha" == "$cur_sha" ]]; then
      head_cache_ok=1
      echo "[prepare] head cache hit: $cache_head ($cur_sha)" >>"$LOG_TXT"
    fi
  fi
  if [[ "$head_cache_ok" -ne 1 ]]; then
    if [[ -f "$cache_head" ]]; then
      echo "[prepare] head cache present but sha mismatch/missing; re-downloading" >>"$LOG_TXT"
    fi
    if download_to "$head_url" "$cache_head"; then
      head_sha="$(sha256_file "$cache_head" 2>/dev/null || true)"
      if [[ -n "$head_sha" ]]; then
        echo "$head_sha" >"$cache_head_sha"
      fi
    else
      echo "[prepare] head download failed" >>"$LOG_TXT"
      if [[ -f "$cache_head" ]]; then
        head_sha="$(sha256_file "$cache_head" 2>/dev/null || true)"
        if [[ -n "$head_sha" ]]; then
          echo "[prepare] proceeding with existing cached head (offline reuse): $cache_head" >>"$LOG_TXT"
          echo "$head_sha" >"$cache_head_sha"
        else
          failure_category="download_failed"
          status="failure"
          exit_code="1"
        fi
      else
        failure_category="download_failed"
        status="failure"
        exit_code="1"
      fi
    fi
  fi

  if [[ "$exit_code" -eq 0 ]]; then
    # Copy into stable filenames under benchmark_assets/model/
    backbone_ckpt_abs="$model_root_abs/backbone.pth"
    head_ckpt_abs="$model_root_abs/head.pth"
    cp -f "$cache_backbone" "$backbone_ckpt_abs"
    cp -f "$cache_head" "$head_ckpt_abs"
    if [[ ! -s "$backbone_ckpt_abs" || ! -s "$head_ckpt_abs" ]]; then
      echo "[prepare] model files copied but not found/empty under $model_root_abs" >>"$LOG_TXT"
      failure_category="model"
      status="failure"
      exit_code="1"
    else
      # Work around upstream checkpoint mismatch: model head has linear_bias=False by default, but some
      # published head checkpoints include head.linear.bias. Remove it to allow strict loading.
      py_torch="$(resolve_python_for_torch || true)"
      if [[ -n "$py_torch" ]]; then
        if ! sanitize_head_checkpoint_inplace "$head_ckpt_abs" "$py_torch"; then
          echo "[prepare] failed to sanitize head checkpoint; cannot proceed" >>"$LOG_TXT"
          failure_category="model"
          status="failure"
          exit_code="1"
        fi
      else
        echo "[prepare] unable to resolve a python executable to sanitize head checkpoint" >>"$LOG_TXT"
        failure_category="deps"
        status="failure"
        exit_code="1"
      fi

      if [[ "$exit_code" -eq 0 ]]; then
        backbone_sha="$(sha256_file "$backbone_ckpt_abs" 2>/dev/null || true)"
        head_sha="$(sha256_file "$head_ckpt_abs" 2>/dev/null || true)"
        if [[ -z "$backbone_sha" || -z "$head_sha" ]]; then
          echo "[prepare] unable to compute model sha256" >>"$LOG_TXT"
          failure_category="model"
          status="failure"
          exit_code="1"
        else
          model_sha256="$(sha256_text "${backbone_sha}\n${head_sha}\n")"
          echo "[prepare] model prepared at: $model_root_abs" >>"$LOG_TXT"
          echo "[prepare] model sha256 (combined)=$model_sha256" >>"$LOG_TXT"
        fi
      fi
    fi
  fi
fi

# --- Write manifest for downstream stages ---
if [[ "$exit_code" -eq 0 ]]; then
  cat >"$MANIFEST_ENV" <<EOF
# Generated by benchmark_scripts/prepare_assets.sh
DATASET_ROOT_REL=$(printf '%q' "$dataset_root_rel")
DATASET_ROOT_ABS=$(printf '%q' "$dataset_root_abs")
MODEL_ROOT_REL=$(printf '%q' "$model_root_rel")
MODEL_ROOT_ABS=$(printf '%q' "$model_root_abs")
MODEL_NAME=$(printf '%q' "$model_name")
PROBE_LAYERS=$(printf '%q' "$probe_layers")
BACKBONE_CKPT_ABS=$(printf '%q' "$backbone_ckpt_abs")
HEAD_CKPT_ABS=$(printf '%q' "$head_ckpt_abs")
EOF
  echo "[prepare] wrote manifest: $MANIFEST_ENV" >>"$LOG_TXT"
  status="success"
  exit_code="0"
  failure_category="unknown"
fi

STAGE_STATUS="$status" STAGE_EXIT_CODE="$exit_code" STAGE_FAILURE_CATEGORY="$failure_category" \
STAGE_SKIP_REASON="$skip_reason" STAGE_COMMAND="$command_str" STAGE_ERROR_EXCERPT="$(tail -n 220 "$LOG_TXT" 2>/dev/null || true)" \
ASSET_DATASET_PATH="$dataset_root_abs" ASSET_DATASET_SOURCE="$dataset_source" ASSET_DATASET_VERSION="$dataset_version" ASSET_DATASET_SHA256="$dataset_sha256" \
ASSET_MODEL_PATH="$model_root_abs" ASSET_MODEL_SOURCE="$model_source" ASSET_MODEL_VERSION="$model_version" ASSET_MODEL_SHA256="$model_sha256" \
META_PYTHON="$python_exe" META_GIT_COMMIT="$git_commit" META_DECISION_REASON="$decision_reason" \
META_MODEL_NAME="$model_name" META_PROBE_LAYERS="$probe_layers" META_BACKBONE_CKPT="$backbone_ckpt_abs" META_HEAD_CKPT="$head_ckpt_abs" \
META_HEAD_SANITIZED="$head_sanitized" META_HEAD_SANITIZED_KEYS="$head_sanitized_removed_keys" \
  write_results

exit "$exit_code"
