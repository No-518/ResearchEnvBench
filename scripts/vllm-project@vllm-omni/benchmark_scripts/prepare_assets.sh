#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model) under benchmark_assets/.

Outputs (always):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Optional:
  --python <path>            Explicit python executable to use
  --report-path <path>       Override report.json path (default: /opt/scimlopsbench/report.json)
  --model-id <repo_id>       Default: Tongyi-MAI/Z-Image-Turbo
  --model-revision <rev>     Default: main
  --prompt <text>            Default: a cup of coffee on the table
  --timeout-sec <n>          Default: 1200 (best-effort; download may still run longer if timeout missing)

Environment:
  HF_AUTH_TOKEN              Optional HF token (will be forwarded to huggingface_hub)
  SCIMLOPSBENCH_OFFLINE=1    Force offline mode (use cache only)
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python_bin=""
report_path=""
model_id="Tongyi-MAI/Z-Image-Turbo"
model_revision="main"
prompt="a cup of coffee on the table"
timeout_sec="1200"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_bin="${2:-}"; shift 2 ;;
    --report-path)
      report_path="${2:-}"; shift 2 ;;
    --model-id)
      model_id="${2:-}"; shift 2 ;;
    --model-revision)
      model_revision="${2:-}"; shift 2 ;;
    --prompt)
      prompt="${2:-}"; shift 2 ;;
    --timeout-sec)
      timeout_sec="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

cd "$repo_root"

out_dir="build_output/prepare"
mkdir -p "$out_dir"
log_path="$out_dir/log.txt"
results_json="$out_dir/results.json"

exec > >(tee -a "$log_path") 2>&1

echo "[prepare] repo_root=$repo_root"
echo "[prepare] model_id=$model_id revision=$model_revision"

stage_status="failure"
stage_exit_code=1
failure_category="unknown"
skip_reason="unknown"
command_str=""

git_commit=""
if command -v git >/dev/null 2>&1; then
  git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
fi

report_path_resolved="${report_path:-${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}}"

resolve_report_python() {
  local rp="$1"
  local py_exec=""
  if command -v python3 >/dev/null 2>&1; then
    py_exec="python3"
  elif command -v python >/dev/null 2>&1; then
    py_exec="python"
  else
    return 1
  fi
  "$py_exec" - "$rp" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if not path.exists():
    sys.exit(2)
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(3)
val = data.get("python_path")
if not isinstance(val, str) or not val:
    sys.exit(4)
print(val)
PY
}

python_source=""
python_warning=""
if [[ -n "$python_bin" ]]; then
  python_source="cli"
else
  if py_from_report="$(resolve_report_python "$report_path_resolved" 2>/dev/null)"; then
    python_bin="$py_from_report"
    python_source="report"
  else
    failure_category="missing_report"
    python_warning="python_path unavailable from report; provide --python"
  fi
fi

echo "[prepare] report_path=$report_path_resolved"
echo "[prepare] python=$python_bin (source=$python_source)"
if [[ -n "$python_warning" ]]; then
  echo "[prepare] python_warning=$python_warning"
fi

assets_root="benchmark_assets"
cache_root="$assets_root/cache"
dataset_root="$assets_root/dataset"
model_root="$assets_root/model"

mkdir -p "$cache_root" "$dataset_root" "$model_root"
mkdir -p "$out_dir/tmp"

# Keep all caches within benchmark_assets/cache (best-effort).
export HF_HOME="$repo_root/$cache_root/hf_home"
export HF_HUB_CACHE="$HF_HOME/hub"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export DIFFUSERS_CACHE="$HF_HOME/diffusers"
export TORCH_HOME="$repo_root/$cache_root/torch"
export XDG_CACHE_HOME="$repo_root/$cache_root/xdg"
export TMPDIR="$repo_root/$out_dir/tmp"

if [[ -n "${HF_AUTH_TOKEN:-}" && -z "${HUGGINGFACE_HUB_TOKEN:-}" ]]; then
  export HUGGINGFACE_HUB_TOKEN="$HF_AUTH_TOKEN"
fi

dataset_path="$dataset_root/prompts.txt"
printf '%s\n' "$prompt" > "$dataset_path"

dataset_sha256=""
if command -v sha256sum >/dev/null 2>&1; then
  dataset_sha256="$(sha256sum "$dataset_path" | awk '{print $1}')"
fi

sanitize_id() {
  echo "$1" | tr '/:' '__' | tr -cd 'A-Za-z0-9._-'
}

model_id_sanitized="$(sanitize_id "$model_id")"
model_cache_dir="$cache_root/models/$model_id_sanitized"
model_link_dir="$model_root/$model_id_sanitized"

mkdir -p "$model_cache_dir"

prepare_prev_sha=""
prepare_prev_model_id=""
prepare_prev_model_revision=""
prepare_prev_model_version=""
prepare_prev_model_path=""
if [[ -f "$results_json" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    prepare_prev_sha="$(python3 - "$results_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print((data.get("assets") or {}).get("model", {}).get("sha256", "") or "")
except Exception:
    pass
PY
)"
    prepare_prev_model_id="$(python3 - "$results_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print((data.get("meta") or {}).get("requested_model_id", "") or "")
except Exception:
    pass
PY
)"
    prepare_prev_model_revision="$(python3 - "$results_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print((data.get("meta") or {}).get("requested_model_revision", "") or "")
except Exception:
    pass
PY
)"
    prepare_prev_model_version="$(python3 - "$results_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print((data.get("assets") or {}).get("model", {}).get("version", "") or "")
except Exception:
    pass
PY
)"
    prepare_prev_model_path="$(python3 - "$results_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print((data.get("assets") or {}).get("model", {}).get("path", "") or "")
except Exception:
    pass
PY
)"
  fi
fi

download_info_json="$out_dir/model_download_info.json"
rm -f "$download_info_json"

if [[ "$failure_category" == "unknown" ]]; then
  compute_manifest_sha() {
    local root="$1"
    if ! command -v python3 >/dev/null 2>&1; then
      return 1
    fi
    python3 - "$root" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
entries = []
for p in sorted(root.rglob("*")):
    if p.is_file():
        rel = p.relative_to(root).as_posix()
        try:
            size = p.stat().st_size
        except OSError:
            size = -1
        entries.append([rel, size])
h = hashlib.sha256()
h.update(json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
print(h.hexdigest())
PY
  }

  cache_hit=0
  current_manifest_sha=""
  if [[ -n "$prepare_prev_sha" && "$prepare_prev_model_id" == "$model_id" && "$prepare_prev_model_revision" == "$model_revision" ]]; then
    if [[ -d "$model_cache_dir" ]] && [[ -n "$(ls -A "$model_cache_dir" 2>/dev/null || true)" ]]; then
      if current_manifest_sha="$(compute_manifest_sha "$model_cache_dir" 2>/dev/null)"; then
        if [[ -n "$current_manifest_sha" && "$current_manifest_sha" == "$prepare_prev_sha" ]]; then
          cache_hit=1
        fi
      fi
    fi
  fi

  if [[ "$cache_hit" -eq 1 ]]; then
    echo "[prepare] cache hit for $model_id@$model_revision (sha256_manifest=$current_manifest_sha); skipping download"
    command_str="cache_hit(model=$model_id revision=$model_revision)"
    cat >"$download_info_json" <<JSON
{"repo_id":"$model_id","requested_revision":"$model_revision","resolved_revision":"$prepare_prev_model_version","local_dir":"$repo_root/$model_cache_dir","downloaded":false,"offline":${SCIMLOPSBENCH_OFFLINE:-0},"auth_used":false,"sha256_manifest":"$current_manifest_sha","warnings":["cache_hit_skip_download"],"error":"","error_type":""}
JSON
  else
    command_str="$python_bin -c 'huggingface_hub.snapshot_download(...)'"
    echo "[prepare] downloading model via huggingface_hub into $model_cache_dir"

    hf_download_py="$(cat <<'PY'
import hashlib
import json
import os
import pathlib
import sys
import traceback

repo_id = sys.argv[1]
revision = sys.argv[2]
local_dir = pathlib.Path(sys.argv[3]).resolve()

token = os.environ.get("HUGGINGFACE_HUB_TOKEN") or os.environ.get("HF_TOKEN") or os.environ.get("HF_AUTH_TOKEN")
offline = os.environ.get("SCIMLOPSBENCH_OFFLINE") == "1" or os.environ.get("HF_HUB_OFFLINE") == "1"

def manifest_sha256(root: pathlib.Path) -> str:
    entries = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            try:
                size = p.stat().st_size
            except OSError:
                size = -1
            entries.append([rel, size])
    h = hashlib.sha256()
    h.update(json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
    return h.hexdigest()

def dir_nonempty(root: pathlib.Path) -> bool:
    try:
        next(root.iterdir())
        return True
    except StopIteration:
        return False
    except Exception:
        return False

out = {
    "repo_id": repo_id,
    "requested_revision": revision,
    "resolved_revision": "",
    "local_dir": str(local_dir),
    "downloaded": False,
    "offline": offline,
    "auth_used": bool(token),
    "sha256_manifest": "",
    "warnings": [],
    "error": "",
    "error_type": "",
}

try:
    from huggingface_hub import HfApi, snapshot_download
except Exception as e:
    out["error"] = f"Failed to import huggingface_hub: {e}"
    out["error_type"] = "deps"
    print(json.dumps(out))
    sys.exit(10)

try:
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    local_dir.mkdir(parents=True, exist_ok=True)

    # Attempt to resolve revision (best-effort).
    try:
        api = HfApi()
        info = api.model_info(repo_id=repo_id, revision=revision, token=token)
        sha = getattr(info, "sha", "") or ""
        if isinstance(sha, str):
            out["resolved_revision"] = sha
    except Exception as e:
        out["warnings"].append(f"model_info_failed: {type(e).__name__}: {e}")

    path = snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=str(local_dir),
        local_dir_use_symlinks=False,
        resume_download=True,
        token=token,
    )
    out["downloaded"] = True
    out["local_dir"] = str(path)
except Exception as e:
    out["error"] = f"{type(e).__name__}: {e}"
    out["error_type"] = "download_failed"
    # If we are offline or network failed but cache exists, allow reuse.
    if dir_nonempty(local_dir):
        out["warnings"].append("download_failed_but_cache_present: proceeding with cached local_dir")
        out["downloaded"] = False
        out["error"] = ""
        out["error_type"] = ""
    else:
        # Auth failures are common; try to classify.
        msg = str(e)
        if "401" in msg or "403" in msg or "Unauthorized" in msg or "Repository not found" in msg:
            out["error_type"] = "auth_required"

if dir_nonempty(local_dir):
    out["sha256_manifest"] = manifest_sha256(local_dir)
else:
    if not out["error_type"]:
        out["error_type"] = "model"
        out["error"] = f"Model directory is empty or missing: {local_dir}"

print(json.dumps(out))
sys.exit(0 if not out["error_type"] else 11)
PY
)"

    run_snapshot_download() {
      local out_json="$1"
      set +e
      if command -v timeout >/dev/null 2>&1; then
        timeout --preserve-status "$timeout_sec" "$python_bin" - "$model_id" "$model_revision" "$repo_root/$model_cache_dir" >"$out_json" <<<"$hf_download_py"
        rc=$?
      else
        "$python_bin" - "$model_id" "$model_revision" "$repo_root/$model_cache_dir" >"$out_json" <<<"$hf_download_py"
        rc=$?
      fi
      set -e
      return "$rc"
    }

    get_error_type() {
      if ! command -v python3 >/dev/null 2>&1; then
        return 0
      fi
      python3 - "$download_info_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    d=json.load(open(sys.argv[1],encoding='utf-8'))
    print(d.get('error_type','') or '')
except Exception:
    pass
PY
    }

    dl_rc=0
    run_snapshot_download "$download_info_json" || dl_rc=$?

    echo "[prepare] model_download_rc=$dl_rc"
    if [[ "$dl_rc" -ne 0 ]]; then
      if [[ -f "$download_info_json" ]]; then
        error_type="$(get_error_type)"
        case "$error_type" in
          auth_required)
            failure_category="auth_required" ;;
          deps)
            failure_category="deps" ;;
          model)
            failure_category="model" ;;
          download_failed|*)
            failure_category="download_failed" ;;
        esac
      else
        failure_category="download_failed"
      fi
    fi

    # If auth is required and no token is configured, prompt once and retry.
    if [[ "$failure_category" == "auth_required" && -z "${HUGGINGFACE_HUB_TOKEN:-}" && -z "${HF_AUTH_TOKEN:-}" ]]; then
      if [[ "${SCIMLOPSBENCH_OFFLINE:-}" == "1" ]]; then
        echo "[prepare] auth required but SCIMLOPSBENCH_OFFLINE=1; not prompting"
      elif [[ -t 0 ]]; then
        echo "[prepare] Hugging Face auth is required to download $model_id."
        echo "[prepare] Set HF_AUTH_TOKEN/HUGGINGFACE_HUB_TOKEN or paste a token now (input hidden)."
        read -r -s -p "HF token: " token
        echo
        if [[ -n "$token" ]]; then
          export HUGGINGFACE_HUB_TOKEN="$token"
          export HF_AUTH_TOKEN="$token"
          echo "[prepare] retrying download with provided token..."
          failure_category="unknown"
          dl_rc=0
          run_snapshot_download "$download_info_json" || dl_rc=$?
          echo "[prepare] retry_model_download_rc=$dl_rc"
          if [[ "$dl_rc" -ne 0 ]]; then
            error_type="$(get_error_type)"
            case "$error_type" in
              deps) failure_category="deps" ;;
              model) failure_category="model" ;;
              auth_required) failure_category="auth_required" ;;
              download_failed|*) failure_category="download_failed" ;;
            esac
          fi
        else
          echo "[prepare] empty token; keeping auth_required failure"
        fi
      else
        echo "[prepare] auth required but stdin is not a TTY; not prompting"
      fi
    fi
  fi
fi

model_resolved_path=""
model_sha256=""
model_version=""
model_source="huggingface"
downloaded_flag=""
offline_flag=""

if [[ -f "$download_info_json" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    model_resolved_path="$(python3 - "$download_info_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    d=json.load(open(sys.argv[1],encoding='utf-8'))
    print(d.get('local_dir','') or '')
except Exception:
    pass
PY
)"
    model_sha256="$(python3 - "$download_info_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    d=json.load(open(sys.argv[1],encoding='utf-8'))
    print(d.get('sha256_manifest','') or '')
except Exception:
    pass
PY
)"
    model_version="$(python3 - "$download_info_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    d=json.load(open(sys.argv[1],encoding='utf-8'))
    print((d.get('resolved_revision') or d.get('requested_revision') or '') or '')
except Exception:
    pass
PY
)"
    downloaded_flag="$(python3 - "$download_info_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    d=json.load(open(sys.argv[1],encoding='utf-8'))
    print('1' if d.get('downloaded') else '0')
except Exception:
    pass
PY
)"
    offline_flag="$(python3 - "$download_info_json" <<'PY' 2>/dev/null || true
import json, sys
try:
    d=json.load(open(sys.argv[1],encoding='utf-8'))
    print('1' if d.get('offline') else '0')
except Exception:
    pass
PY
)"
  fi
fi

if [[ "$failure_category" == "unknown" ]]; then
  if [[ -z "$model_resolved_path" || ! -d "$model_resolved_path" ]]; then
    echo "[prepare] model resolved path missing or not a directory: $model_resolved_path"
    failure_category="model"
  fi
fi

link_note=""
if [[ "$failure_category" == "unknown" ]]; then
  rm -rf "$model_link_dir"
  if ln -s "$(realpath "$model_resolved_path")" "$model_link_dir" 2>/dev/null; then
    link_note="symlink"
  else
    echo "[prepare] symlink failed; copying model directory (may be large)"
    cp -a "$model_resolved_path" "$model_link_dir"
    link_note="copy"
  fi
fi

if [[ "$failure_category" == "unknown" ]]; then
  stage_status="success"
  stage_exit_code=0
  skip_reason="not_applicable"
else
  stage_status="failure"
  stage_exit_code=1
fi

error_excerpt=""
if [[ "$stage_exit_code" -ne 0 ]]; then
  if command -v tail >/dev/null 2>&1; then
    error_excerpt="$(tail -n 220 "$log_path" || true)"
  fi
fi

python_meta_version=""
if [[ -n "$python_bin" ]]; then
  python_meta_version="$("$python_bin" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
fi

cat >"$results_json" <<JSON
{
  "status": "$(printf '%s' "$stage_status")",
  "skip_reason": "$(printf '%s' "$skip_reason")",
  "exit_code": $stage_exit_code,
  "stage": "prepare",
  "task": "download",
  "command": "$(printf '%s' "$command_str" | sed 's/"/\\"/g')",
  "timeout_sec": $timeout_sec,
  "framework": "unknown",
  "assets": {
    "dataset": {
      "path": "$(printf '%s' "$dataset_path" | sed 's/"/\\"/g')",
      "source": "generated",
      "version": "v1",
      "sha256": "$(printf '%s' "$dataset_sha256" | sed 's/"/\\"/g')"
    },
    "model": {
      "path": "$(printf '%s' "$model_link_dir" | sed 's/"/\\"/g')",
      "source": "$(printf '%s' "$model_source" | sed 's/"/\\"/g')",
      "version": "$(printf '%s' "$model_version" | sed 's/"/\\"/g')",
      "sha256": "$(printf '%s' "$model_sha256" | sed 's/"/\\"/g')"
    }
  },
  "meta": {
    "python": "$(printf '%s' "$python_bin" | sed 's/"/\\"/g')",
    "python_version": "$(printf '%s' "$python_meta_version" | sed 's/"/\\"/g')",
    "python_source": "$(printf '%s' "$python_source" | sed 's/"/\\"/g')",
    "python_warning": "$(printf '%s' "$python_warning" | sed 's/"/\\"/g')",
    "requested_model_id": "$(printf '%s' "$model_id" | sed 's/"/\\"/g')",
    "requested_model_revision": "$(printf '%s' "$model_revision" | sed 's/"/\\"/g')",
    "git_commit": "$(printf '%s' "$git_commit" | sed 's/"/\\"/g')",
    "env_vars": {
      "HF_HOME": "$(printf '%s' "${HF_HOME:-}" | sed 's/"/\\"/g')",
      "HF_HUB_CACHE": "$(printf '%s' "${HF_HUB_CACHE:-}" | sed 's/"/\\"/g')",
      "TRANSFORMERS_CACHE": "$(printf '%s' "${TRANSFORMERS_CACHE:-}" | sed 's/"/\\"/g')",
      "DIFFUSERS_CACHE": "$(printf '%s' "${DIFFUSERS_CACHE:-}" | sed 's/"/\\"/g')",
      "TORCH_HOME": "$(printf '%s' "${TORCH_HOME:-}" | sed 's/"/\\"/g')",
      "XDG_CACHE_HOME": "$(printf '%s' "${XDG_CACHE_HOME:-}" | sed 's/"/\\"/g')",
      "TMPDIR": "$(printf '%s' "${TMPDIR:-}" | sed 's/"/\\"/g')",
      "SCIMLOPSBENCH_OFFLINE": "$(printf '%s' "${SCIMLOPSBENCH_OFFLINE:-}" | sed 's/"/\\"/g')"
    },
    "decision_reason": "Selected official offline inference entrypoint examples/offline_inference/text_to_image/text_to_image.py; default model Tongyi-MAI/Z-Image-Turbo from docs/getting_started/quickstart.md; dataset is a single-prompt file.",
    "model_download": {
      "cache_dir": "$(printf '%s' "$model_cache_dir" | sed 's/"/\\"/g')",
      "resolved_dir": "$(printf '%s' "$model_resolved_path" | sed 's/"/\\"/g')",
      "linked_as": "$(printf '%s' "$link_note" | sed 's/"/\\"/g')",
      "downloaded": $( [[ "$downloaded_flag" == "1" ]] && echo true || echo false ),
      "offline": $( [[ "$offline_flag" == "1" ]] && echo true || echo false ),
      "sha256_note": "sha256 is computed from a deterministic file manifest (relative paths + sizes), not raw file bytes."
    }
  },
  "failure_category": "$(printf '%s' "$failure_category" | sed 's/"/\\"/g')",
  "error_excerpt": "$(printf '%s' "$error_excerpt" | sed 's/\\/\\\\/g; s/\"/\\\"/g')"
}
JSON

echo "[prepare] status=$stage_status exit_code=$stage_exit_code failure_category=$failure_category"
exit "$stage_exit_code"
