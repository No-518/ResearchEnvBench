#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets for TGATE:
- Create a tiny "dataset" (prompt file) under benchmark_assets/dataset/
- Download one minimal supported diffusers model snapshot under benchmark_assets/cache/

Outputs:
  build_output/prepare/log.txt
  build_output/prepare/results.json

Options:
  --report-path <path>   Default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json
  --python <path>        Explicit python executable (overrides report resolution)
  --model <key>          Force one of: pixart_alpha|pixart_sigma|lcm_pixart|sdxl|lcm_sdxl|svd
  --offline              Do not attempt network; use cache only
EOF
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="$repo_root/build_output/prepare"
assets_root="$repo_root/benchmark_assets"
cache_root="$assets_root/cache"
dataset_dir="$assets_root/dataset"
model_dir="$assets_root/model"

report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
python_override=""
forced_model=""
offline=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_override="${2:-}"; shift 2 ;;
    --model) forced_model="${2:-}"; shift 2 ;;
    --offline) offline=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

mkdir -p "$out_dir" "$cache_root" "$dataset_dir" "$model_dir"
log_txt="$out_dir/log.txt"
results_json="$out_dir/results.json"

: >"$log_txt"
exec > >(tee -a "$log_txt") 2>&1

bootstrap_py="$(command -v python3 || command -v python || true)"

status="failure"
exit_code=1
failure_category="unknown"
skip_reason="unknown"
command_str="benchmark_scripts/prepare_assets.sh"
decision_reason="Select a TGATE-supported diffusers model that can be downloaded anonymously; prepare a 1-prompt dataset."
git_commit="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)"

echo "[prepare] repo_root=$repo_root"
echo "[prepare] report_path=$report_path"
echo "[prepare] out_dir=$out_dir"
echo "[prepare] cache_root=$cache_root"

if [[ -z "$bootstrap_py" ]]; then
  echo "[prepare] python3/python not found in PATH; cannot proceed." >&2
  failure_category="deps"
  cat >"$results_json" <<'JSON'
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "python3/python not found in PATH"
  },
  "failure_category": "deps",
  "error_excerpt": "python3/python not found in PATH"
}
JSON
  exit 1
fi

resolved_python="$python_override"
if [[ -z "$resolved_python" ]]; then
  resolved_python="$("$bootstrap_py" "$repo_root/benchmark_scripts/runner.py" --stage prepare --task download --print-python --report-path "$report_path" --requires-python 2>/dev/null || true)"
fi

if [[ -z "$resolved_python" ]]; then
  echo "[prepare] Could not resolve python from report (and --python not provided)." >&2
  "$bootstrap_py" - <<PY >"$results_json" 2>/dev/null || true
import json
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {"python": "", "git_commit": "$git_commit", "env_vars": {}, "decision_reason": "$decision_reason"},
  "failure_category": "missing_report",
  "error_excerpt": "Could not resolve python from report and no --python provided.",
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
  exit 1
fi

echo "[prepare] resolved_python=$resolved_python"
if [[ ! -f "$resolved_python" ]] || [[ ! -x "$resolved_python" ]]; then
  echo "[prepare] Resolved python is not an executable file: $resolved_python" >&2
  "$bootstrap_py" - <<PY >"$results_json" 2>/dev/null || true
import json
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": 1200,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {"python": "$resolved_python", "git_commit": "$git_commit", "env_vars": {}, "decision_reason": "$decision_reason"},
  "failure_category": "missing_report",
  "error_excerpt": "Resolved python is not executable.",
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
  exit 1
fi

# Route common caches into benchmark_assets/cache to avoid writing elsewhere.
export HF_HOME="$cache_root/hf_home"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export DIFFUSERS_CACHE="$HF_HOME/diffusers"
export HF_DATASETS_CACHE="$HF_HOME/datasets"
export XDG_CACHE_HOME="$cache_root/xdg"
export TORCH_HOME="$cache_root/torch"
export PIP_CACHE_DIR="$cache_root/pip"
export HF_HUB_DISABLE_TELEMETRY=1
export TOKENIZERS_PARALLELISM=false
mkdir -p "$HF_HOME" "$HUGGINGFACE_HUB_CACHE" "$TRANSFORMERS_CACHE" "$DIFFUSERS_CACHE" "$HF_DATASETS_CACHE" "$XDG_CACHE_HOME" "$TORCH_HOME" "$PIP_CACHE_DIR"

if [[ "$offline" -eq 1 ]]; then
  export HF_HUB_OFFLINE=1
fi

FORCED_MODEL="$forced_model" OFFLINE="$offline" REPO_ROOT="$repo_root" OUT_DIR="$out_dir" \
ASSETS_ROOT="$assets_root" DATASET_DIR="$dataset_dir" MODEL_DIR="$model_dir" CACHE_ROOT="$cache_root" \
GIT_COMMIT="$git_commit" DECISION_REASON="$decision_reason" \
"$resolved_python" - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


repo_root = Path(os.environ["REPO_ROOT"]).resolve()
out_dir = Path(os.environ["OUT_DIR"]).resolve()
assets_root = Path(os.environ["ASSETS_ROOT"]).resolve()
dataset_dir = Path(os.environ["DATASET_DIR"]).resolve()
model_dir = Path(os.environ["MODEL_DIR"]).resolve()
cache_root = Path(os.environ["CACHE_ROOT"]).resolve()
forced_model = os.environ.get("FORCED_MODEL", "").strip()
offline = os.environ.get("OFFLINE", "0").strip() == "1"
git_commit = os.environ.get("GIT_COMMIT", "")
decision_reason = os.environ.get("DECISION_REASON", "")

ensure_dir(out_dir)
ensure_dir(dataset_dir)
ensure_dir(model_dir)

results_path = out_dir / "results.json"
log_path = out_dir / "log.txt"

result: dict[str, Any] = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "prepare",
    "task": "download",
    "command": "benchmark_scripts/prepare_assets.sh",
    "timeout_sec": 1200,
    "framework": "pytorch",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": sys.executable,
        "git_commit": git_commit,
        "env_vars": {},
        "decision_reason": decision_reason,
        "timestamp_utc": utc_now_iso(),
        "warnings": [],
    },
    "failure_category": "unknown",
    "error_excerpt": "",
}

try:
    # Dataset: 1 prompt file + a repo-local image copy for possible SVD runs.
    prompt_text = "Astronaut in a jungle, cold color palette, muted colors, detailed, 8k"
    prompts_path = dataset_dir / "prompts.txt"
    prompts_path.write_text(prompt_text + "\n", encoding="utf-8")
    dataset_sha = sha256_file(prompts_path)

    # Copy a small repo-local image as an optional "dataset" item for SVD (no network).
    repo_teaser = repo_root / "assets" / "teaser.png"
    image_path = dataset_dir / "teaser.png"
    if repo_teaser.is_file():
        shutil.copy2(repo_teaser, image_path)

    # Model candidates: choose the first that can be resolved/downloaded anonymously.
    # Note: main.py hardcodes these repo ids; we cannot substitute smaller checkpoints.
    candidates: list[dict[str, Any]] = [
        {"key": "pixart_alpha", "repos": ["PixArt-alpha/PixArt-XL-2-1024-MS"], "input": "prompt"},
        {"key": "pixart_sigma", "repos": ["PixArt-alpha/PixArt-Sigma-XL-2-1024-MS"], "input": "prompt"},
        {"key": "lcm_pixart", "repos": ["PixArt-alpha/PixArt-LCM-XL-2-1024-MS"], "input": "prompt"},
        {"key": "sdxl", "repos": ["stabilityai/stable-diffusion-xl-base-1.0"], "input": "prompt"},
        {"key": "lcm_sdxl", "repos": ["stabilityai/stable-diffusion-xl-base-1.0", "latent-consistency/lcm-sdxl"], "input": "prompt"},
        {"key": "svd", "repos": ["stabilityai/stable-video-diffusion-img2vid-xt"], "input": "image"},
    ]

    if forced_model:
        forced = [c for c in candidates if c["key"] == forced_model]
        if not forced:
            raise RuntimeError(f"Unknown --model '{forced_model}'. Valid: {[c['key'] for c in candidates]}")
        candidates = forced

    try:
        from huggingface_hub import HfApi, snapshot_download
        from huggingface_hub.utils import HfHubHTTPError, LocalEntryNotFoundError
    except Exception as e:
        raise RuntimeError(f"huggingface_hub is required for model downloads: {e}") from e

    token = os.environ.get("HF_TOKEN") or os.environ.get("HF_AUTH_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    use_token = bool(token)
    token_arg = token if use_token else False  # enforce anonymous by default

    cache_dir = Path(os.environ.get("HUGGINGFACE_HUB_CACHE", str(cache_root / "hf_home" / "hub"))).resolve()
    ensure_dir(cache_dir)

    previous = load_json(results_path)
    if previous and previous.get("status") == "success":
        prev_model_path = previous.get("assets", {}).get("model", {}).get("path")
        prev_dataset_path = previous.get("assets", {}).get("dataset", {}).get("path")
        prev_model_sha = previous.get("assets", {}).get("model", {}).get("sha256")
        prev_dataset_sha = previous.get("assets", {}).get("dataset", {}).get("sha256")
        if prev_model_path and prev_dataset_path:
            mp = Path(str(prev_model_path))
            dp = Path(str(prev_dataset_path))
            if mp.exists() and dp.exists():
                cur_dataset_sha = sha256_file(dp) if dp.is_file() else ""
                if prev_dataset_sha == cur_dataset_sha and prev_model_sha:
                    result["status"] = "success"
                    result["exit_code"] = 0
                    result["assets"] = previous.get("assets", result["assets"])
                    result["meta"]["selected"] = previous.get("meta", {}).get("selected", {})
                    result["meta"]["decision_reason"] = "Reused existing assets (sha256 match) from prior prepare run."
                    result["failure_category"] = ""
                    write_json(results_path, result)
                    sys.exit(0)

    auth_required: list[str] = []
    download_failures: list[str] = []

    selected: dict[str, Any] | None = None
    downloaded_paths: dict[str, str] = {}
    downloaded_versions: dict[str, str] = {}

    api = HfApi()

    for cand in candidates:
        key = cand["key"]
        repos = cand["repos"]
        needs = cand["input"]
        ok = True
        downloaded_paths = {}
        downloaded_versions = {}

        # Quick access check (best-effort) to prefer non-gated models.
        if not offline and not use_token:
            try:
                for rid in repos:
                    api.model_info(rid, token=False)
            except Exception as e:
                # If it looks like gated, mark and skip; otherwise proceed to snapshot_download for better signal.
                msg = str(e)
                if "403" in msg or "401" in msg:
                    auth_required.append(f"{key}: {repos} (access denied)")
                    ok = False
                else:
                    result["meta"]["warnings"].append(f"model_info check failed for {repos}: {msg}")
        if not ok:
            continue

        for rid in repos:
            try:
                path = snapshot_download(
                    repo_id=rid,
                    cache_dir=str(cache_dir),
                    token=token_arg,
                    local_files_only=offline,
                    resume_download=True,
                )
                snap_path = Path(path).resolve()
                if not snap_path.exists():
                    raise RuntimeError(f"snapshot_download returned path that does not exist: {snap_path}")
                downloaded_paths[rid] = str(snap_path)
                downloaded_versions[rid] = snap_path.name
            except Exception as e:
                msg = str(e)
                if "403" in msg or "401" in msg:
                    auth_required.append(f"{key}: {rid}")
                    ok = False
                    break
                # If network appears down, attempt offline reuse.
                try:
                    path = snapshot_download(
                        repo_id=rid,
                        cache_dir=str(cache_dir),
                        token=token_arg,
                        local_files_only=True,
                        resume_download=True,
                    )
                    snap_path = Path(path).resolve()
                    if not snap_path.exists():
                        raise RuntimeError(f"snapshot_download(local_files_only) returned non-existent path: {snap_path}")
                    downloaded_paths[rid] = str(snap_path)
                    downloaded_versions[rid] = snap_path.name
                    result["meta"]["warnings"].append(f"Offline reuse for {rid} after download error: {msg}")
                except Exception:
                    download_failures.append(f"{key}: {rid}: {msg}")
                    ok = False
                    break

        if not ok:
            continue

        selected = {"key": key, "repos": repos, "input": needs}
        break

    if not selected:
        if auth_required and not download_failures:
            raise RuntimeError(
                "All available TGATE model candidates appear to require authentication. "
                "Set HF_TOKEN (or HUGGINGFACE_HUB_TOKEN) after accepting the model license(s), then re-run."
            )
        raise RuntimeError(
            "Failed to download any TGATE model candidate. "
            f"auth_required={auth_required} download_failures={download_failures}"
        )

    # Link/copy into benchmark_assets/model without assuming hub layout: we use the returned snapshot path(s).
    key = selected["key"]
    ensure_dir(model_dir / key)
    stable_model_path = (model_dir / key).resolve()
    for rid, path_str in downloaded_paths.items():
        src = Path(path_str)
        link_name = stable_model_path / rid.replace("/", "__")
        if link_name.exists() or link_name.is_symlink():
            if link_name.is_symlink() or link_name.is_dir():
                try:
                    link_name.unlink()
                except Exception:
                    pass
        try:
            link_name.symlink_to(src, target_is_directory=True)
        except Exception:
            # Fall back to recording a pointer file (avoid expensive copies).
            (link_name.with_suffix(".path")).write_text(str(src), encoding="utf-8")

    model_source = ",".join(selected["repos"])
    model_version = ",".join([downloaded_versions.get(r, "") for r in selected["repos"]])
    model_sha = sha256_text(model_source + "@" + model_version)

    result["assets"]["dataset"] = {
        "path": str(prompts_path),
        "source": "repo_local_prompt",
        "version": "v1",
        "sha256": dataset_sha,
    }
    result["assets"]["model"] = {
        "path": str(stable_model_path),
        "source": f"huggingface:{model_source}",
        "version": model_version,
        "sha256": model_sha,
    }
    result["meta"]["selected"] = {
        "main_model_arg": selected["key"],
        "input_type": selected["input"],
        "prompt_path": str(prompts_path),
        "image_path": str(image_path) if image_path.is_file() else "",
        "snapshot_paths": downloaded_paths,
        "used_hf_token": use_token,
        "offline": offline,
    }
    result["status"] = "success"
    result["exit_code"] = 0
    result["failure_category"] = ""
    write_json(results_path, result)
    sys.exit(0)
except Exception as e:
    tb = traceback.format_exc()
    result["status"] = "failure"
    result["exit_code"] = 1
    msg = str(e)
    if "authentication" in msg.lower() or "hf_token" in msg.lower() or "403" in msg or "401" in msg:
        result["failure_category"] = "auth_required"
    elif "huggingface_hub is required" in msg:
        result["failure_category"] = "deps"
    elif "Failed to download" in msg:
        result["failure_category"] = "download_failed"
    else:
        result["failure_category"] = "unknown"
    result["error_excerpt"] = (msg + "\n" + tb)[-8000:]
    write_json(results_path, result)
    sys.exit(1)
PY

prepare_rc=$?

exit "$prepare_rc"
