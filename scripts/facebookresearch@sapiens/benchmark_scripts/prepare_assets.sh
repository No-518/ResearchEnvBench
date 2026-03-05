#!/usr/bin/env bash
set -u
set -o pipefail

STAGE="prepare"
TASK="download"
FRAMEWORK="unknown"
TIMEOUT_SEC="${SCIMLOPSBENCH_PREPARE_TIMEOUT_SEC:-1200}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${REPO_ROOT}/build_output/${STAGE}"
LOG_PATH="${OUT_DIR}/log.txt"
RESULTS_PATH="${OUT_DIR}/results.json"

mkdir -p "${OUT_DIR}"
TS_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || true)"

# Initialize results.json early to avoid stale artifacts on early termination.
cat > "${RESULTS_PATH}" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "${STAGE}",
  "task": "${TASK}",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": ${TIMEOUT_SEC},
  "framework": "${FRAMEWORK}",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "prepare stage placeholder (not completed)",
    "timestamp_utc": "${TS_UTC}",
    "placeholder": true
  },
  "failure_category": "unknown",
  "error_excerpt": "stage did not complete"
}
EOF
: > "${LOG_PATH}"

exec > >(tee -a "${LOG_PATH}") 2>&1

PY_SYS="$(command -v python3 || command -v python || true)"
if [[ -z "${PY_SYS}" ]]; then
  echo "[prepare] No python found in PATH to resolve report/python." >&2
  # Minimal results.json without python.
  cat > "${RESULTS_PATH}" <<EOF
{
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "${STAGE}",
  "task": "${TASK}",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": ${TIMEOUT_SEC},
  "framework": "${FRAMEWORK}",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""}
  },
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": ""},
  "failure_category": "deps",
  "error_excerpt": "python not found in PATH"
}
EOF
  exit 1
fi

PY_ENV=""
PY_ENV_SOURCE=""
PY_ENV_WARNING=""
REPORT_PATH="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  PY_ENV="${SCIMLOPSBENCH_PYTHON}"
  PY_ENV_SOURCE="env:SCIMLOPSBENCH_PYTHON"
else
  if [[ ! -f "${REPORT_PATH}" ]]; then
    echo "[prepare] Missing report.json: ${REPORT_PATH}" >&2
    "${PY_SYS}" - <<'PY' "${RESULTS_PATH}" "${TIMEOUT_SEC}" "${REPORT_PATH}"
import json, sys, time, os
results_path = sys.argv[1]
timeout_sec = int(sys.argv[2])
report_path = sys.argv[3]
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": timeout_sec,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "prepare stage requires python_path from agent report.json",
    "report_path": report_path,
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  },
  "failure_category": "missing_report",
  "error_excerpt": f"missing report.json: {report_path}",
}
os.makedirs(os.path.dirname(results_path), exist_ok=True)
with open(results_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
    exit 1
  fi

  report_payload="$("${PY_SYS}" - <<'PY' "${REPORT_PATH}" 2>/dev/null || true
import json, sys
p = sys.argv[1]
try:
    data = json.load(open(p, "r", encoding="utf-8"))
    print("OK")
    print(str(data.get("python_path") or ""))
except Exception as e:
    print("ERR")
    print(repr(e))
PY
)"
  report_ok="$(printf "%s\n" "${report_payload}" | sed -n '1p' || true)"
  report_py="$(printf "%s\n" "${report_payload}" | sed -n '2p' || true)"

  if [[ "${report_ok}" != "OK" ]]; then
    echo "[prepare] Invalid report.json: ${REPORT_PATH}" >&2
    "${PY_SYS}" - <<'PY' "${RESULTS_PATH}" "${TIMEOUT_SEC}" "${REPORT_PATH}"
import json, sys, time, os
results_path = sys.argv[1]
timeout_sec = int(sys.argv[2])
report_path = sys.argv[3]
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": timeout_sec,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "prepare stage requires a valid report.json with python_path",
    "report_path": report_path,
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  },
  "failure_category": "missing_report",
  "error_excerpt": f"invalid report.json: {report_path}",
}
os.makedirs(os.path.dirname(results_path), exist_ok=True)
with open(results_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
    exit 1
  fi

  if [[ -n "${report_py}" ]]; then
    PY_ENV="${report_py}"
    PY_ENV_SOURCE="report:python_path"
  else
    PY_ENV="${PY_SYS}"
    PY_ENV_SOURCE="path_fallback"
    PY_ENV_WARNING="python_path missing in report.json; using python from PATH as last resort"
    echo "[prepare] WARNING: ${PY_ENV_WARNING}" >&2
  fi
fi

if [[ ! -x "${PY_ENV}" ]]; then
  echo "[prepare] python_path is not executable: ${PY_ENV}" >&2
  "${PY_SYS}" - <<'PY' "${RESULTS_PATH}" "${TIMEOUT_SEC}" "${PY_ENV}"
import json, sys, time, os
results_path = sys.argv[1]
timeout_sec = int(sys.argv[2])
py_env = sys.argv[3]
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "prepare",
  "task": "download",
  "command": "benchmark_scripts/prepare_assets.sh",
  "timeout_sec": timeout_sec,
  "framework": "unknown",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {
    "python": "",
    "git_commit": "",
    "env_vars": {},
    "decision_reason": "python_path must exist and be executable.",
    "reported_python_path": py_env,
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  },
  "failure_category": "path_hallucination",
  "error_excerpt": f"python_path not executable: {py_env}",
}
os.makedirs(os.path.dirname(results_path), exist_ok=True)
with open(results_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  exit 1
fi

export HF_HOME="${REPO_ROOT}/benchmark_assets/cache/hf_home"
export XDG_CACHE_HOME="${REPO_ROOT}/benchmark_assets/cache/xdg"
export TORCH_HOME="${REPO_ROOT}/benchmark_assets/cache/torch_home"
export HOME="${REPO_ROOT}/benchmark_assets/cache/home"
mkdir -p "${HF_HOME}" "${XDG_CACHE_HOME}" "${TORCH_HOME}" "${HOME}"

DATASET_ID="facebook/sapiens_toy_dataset"
MODEL_ID="facebook/sapiens-pretrain-0.3b"

DATASET_CACHE_DIR="${REPO_ROOT}/benchmark_assets/cache/datasets/sapiens_toy_dataset"
MODEL_CACHE_DIR="${REPO_ROOT}/benchmark_assets/cache/models/facebook_sapiens_pretrain_0.3b"
DATASET_DIR="${REPO_ROOT}/benchmark_assets/dataset/sapiens_toy_dataset_min1"
MODEL_DIR="${REPO_ROOT}/benchmark_assets/model/sapiens_0.3b_pretrain"
MODEL_FILENAME="sapiens_0.3b_epoch_1600_clean.pth"

mkdir -p "${DATASET_CACHE_DIR}" "${MODEL_CACHE_DIR}" "${DATASET_DIR}" "${MODEL_DIR}"

echo "[prepare] Using python_env=${PY_ENV}"
echo "[prepare] Dataset: ${DATASET_ID}"
echo "[prepare] Model: ${MODEL_ID}"

PREPARE_CODE=$(
  cat <<'PY'
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

repo_root = Path(os.environ["REPO_ROOT"]).resolve()
out_dir = Path(os.environ["OUT_DIR"]).resolve()
results_path = out_dir / "results.json"
log_path = out_dir / "log.txt"

dataset_id = os.environ["DATASET_ID"]
model_id = os.environ["MODEL_ID"]
dataset_cache_dir = Path(os.environ["DATASET_CACHE_DIR"]).resolve()
model_cache_dir = Path(os.environ["MODEL_CACHE_DIR"]).resolve()
dataset_dir = Path(os.environ["DATASET_DIR"]).resolve()
model_dir = Path(os.environ["MODEL_DIR"]).resolve()
model_filename = os.environ["MODEL_FILENAME"]
timeout_sec = int(os.environ.get("TIMEOUT_SEC", "1200"))

stage = "prepare"
task = "download"

def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def git_commit() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""

def base_assets():
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

def write_results(payload):
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def tail_log(max_lines=220):
    if not log_path.exists():
        return ""
    try:
        data = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-max_lines:])
    except Exception:
        return ""

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def sha256_dir(root: Path) -> str:
    h = hashlib.sha256()
    files = [p for p in root.rglob("*") if p.is_file()]
    for p in sorted(files, key=lambda x: x.as_posix()):
        rel = p.relative_to(root).as_posix().encode("utf-8")
        h.update(rel + b"\0")
        h.update(str(p.stat().st_size).encode("utf-8") + b"\0")
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()

def ensure_huggingface_hub(meta):
    try:
        import huggingface_hub  # noqa: F401
        meta["huggingface_hub_installed"] = True
        meta["huggingface_hub_install_attempted"] = False
        return
    except Exception as e:
        meta["huggingface_hub_installed"] = False
        meta["huggingface_hub_install_attempted"] = True
        meta["huggingface_hub_import_error"] = repr(e)

    cmd = [sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"]
    meta["huggingface_hub_install_command"] = " ".join(cmd)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    meta["huggingface_hub_install_returncode"] = int(proc.returncode)
    if proc.returncode != 0:
        meta["huggingface_hub_install_stderr"] = (proc.stderr or "")[-2000:]
        raise RuntimeError("failed to install huggingface_hub")
    import huggingface_hub  # noqa: F401
    meta["huggingface_hub_installed"] = True

def download_dataset(meta):
    from huggingface_hub import snapshot_download
    local_dir = dataset_cache_dir
    local_dir.mkdir(parents=True, exist_ok=True)
    meta["dataset_download_mode"] = "online"
    try:
        snapshot_download(
            repo_id=dataset_id,
            repo_type="dataset",
            revision="main",
            local_dir=str(local_dir),
            local_dir_use_symlinks=False,
        )
        return
    except Exception as e:
        meta["dataset_download_error"] = repr(e)
        meta["dataset_download_mode"] = "local_files_only"
        snapshot_download(
            repo_id=dataset_id,
            repo_type="dataset",
            revision="main",
            local_dir=str(local_dir),
            local_dir_use_symlinks=False,
            local_files_only=True,
        )

def prune_dataset_dir(meta, root: Path) -> None:
    img_dir = root / "images"
    mask_dir = root / "masks"
    normal_dir = root / "normals"
    img_files = sorted(
        [
            p
            for p in img_dir.glob("*")
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"}
        ]
    )
    if not img_files:
        raise RuntimeError("no image files found in dataset/images")
    chosen_img = img_files[0]
    stem = chosen_img.stem
    mask_candidates = sorted([p for p in mask_dir.glob(stem + ".*") if p.is_file()])
    normal_candidates = sorted([p for p in normal_dir.glob(stem + ".*") if p.is_file()])
    if not mask_candidates:
        raise RuntimeError(f"no matching mask file for stem={stem} under {mask_dir}")
    if not normal_candidates:
        raise RuntimeError(f"no matching normal file for stem={stem} under {normal_dir}")
    chosen_mask = mask_candidates[0]
    chosen_normal = normal_candidates[0]
    meta["dataset_pruned_sample"] = {
        "image": str(chosen_img.relative_to(root)),
        "mask": str(chosen_mask.relative_to(root)),
        "normal": str(chosen_normal.relative_to(root)),
    }

    def _prune_dir(dir_path: Path, keep: set[Path]) -> None:
        for p in dir_path.glob("*"):
            if p.is_file() and p not in keep:
                try:
                    p.unlink()
                except Exception:
                    pass

    _prune_dir(img_dir, {chosen_img})
    _prune_dir(mask_dir, {chosen_mask})
    _prune_dir(normal_dir, {chosen_normal})

def prepare_dataset(meta):
    dataset_dir.mkdir(parents=True, exist_ok=True)
    required_dirs = [dataset_dir / "images", dataset_dir / "masks", dataset_dir / "normals"]
    if all(p.exists() for p in required_dirs):
        meta["dataset_prepare_mode"] = "reuse_existing_dataset_dir"
        prune_dataset_dir(meta, dataset_dir)
        return
    # Prefer extracting zips if present, otherwise copy known folders or all non-hidden content.
    zips = sorted(dataset_cache_dir.glob("*.zip"))
    if zips:
        meta["dataset_source_layout"] = "zip"
        for z in zips:
            with zipfile.ZipFile(z, "r") as zf:
                zf.extractall(dataset_dir)
    else:
        meta["dataset_source_layout"] = "folder"
        candidates = ["images", "masks", "normals", "depths"]
        copied_any = False
        for name in candidates:
            src = dataset_cache_dir / name
            dst = dataset_dir / name
            if src.is_dir() and not dst.exists():
                shutil.copytree(src, dst)
                copied_any = True
        if not copied_any:
            # Fall back to copying all top-level non-hidden entries.
            for p in dataset_cache_dir.iterdir():
                if p.name.startswith("."):
                    continue
                dst = dataset_dir / p.name
                if p.is_dir():
                    if not dst.exists():
                        shutil.copytree(p, dst)
                elif p.is_file():
                    if not dst.exists():
                        shutil.copy2(p, dst)

    required = [dataset_dir / "images", dataset_dir / "masks", dataset_dir / "normals"]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise RuntimeError("dataset missing required folders: " + ", ".join(missing))
    meta["dataset_prepare_mode"] = "prepared_from_cache"
    prune_dataset_dir(meta, dataset_dir)

def download_model(meta):
    from huggingface_hub import snapshot_download
    model_cache_dir.mkdir(parents=True, exist_ok=True)
    candidates = [
        model_filename,
        "sapiens_0.3b_epoch_1600.pth",
    ]
    meta["model_candidate_paths"] = candidates
    last_err = None
    def _find_downloaded(path_hint: str) -> Path | None:
        direct = model_cache_dir / path_hint
        if direct.is_file():
            return direct
        base = Path(path_hint).name
        if base:
            hits = sorted(model_cache_dir.rglob(base))
            if hits:
                meta["model_download_search_hits"] = [str(p) for p in hits[:5]]
                return hits[0]
        return None

    for cand in candidates:
        for local_only in (False, True):
            meta["model_download_mode"] = "local_files_only" if local_only else "online"
            try:
                snapshot_download(
                    repo_id=model_id,
                    repo_type="model",
                    revision="main",
                    local_dir=str(model_cache_dir),
                    local_dir_use_symlinks=False,
                    allow_patterns=[cand],
                    local_files_only=local_only,
                )
                found = _find_downloaded(cand)
                if found is not None:
                    selected_rel = str(found.relative_to(model_cache_dir))
                    meta["model_selected_path"] = selected_rel
                    return selected_rel
                err = RuntimeError("snapshot_download completed but checkpoint file not found (pattern mismatch?)")
                meta.setdefault("model_candidate_errors", []).append(
                    {f"{cand} ({meta['model_download_mode']})": repr(err)}
                )
                last_err = err
            except Exception as e:
                meta.setdefault("model_candidate_errors", []).append(
                    {f"{cand} ({meta['model_download_mode']})": repr(e)}
                )
                last_err = e

    # Fallback: download by basename pattern (repo layout may differ).
    fallback_patterns = [f"*{model_filename}", "*sapiens_0.3b_epoch_1600.pth"]
    meta["model_fallback_patterns"] = fallback_patterns
    for local_only in (False, True):
        meta["model_download_mode"] = "local_files_only" if local_only else "online"
        try:
            snapshot_download(
                repo_id=model_id,
                repo_type="model",
                revision="main",
                local_dir=str(model_cache_dir),
                local_dir_use_symlinks=False,
                allow_patterns=fallback_patterns,
                local_files_only=local_only,
            )
            for name in (model_filename, "sapiens_0.3b_epoch_1600.pth"):
                hits = sorted(model_cache_dir.rglob(name))
                if hits:
                    meta["model_selected_path"] = str(hits[0].relative_to(model_cache_dir))
                    meta["model_download_search_hits"] = [str(p) for p in hits[:5]]
                    return meta["model_selected_path"]
        except Exception as e:
            meta.setdefault("model_candidate_errors", []).append(
                {f"fallback ({meta['model_download_mode']})": repr(e)}
            )
            last_err = e
    raise RuntimeError(f"failed to download model checkpoint (last_error={last_err!r})")

def locate_model_checkpoint(meta, selected_path: str) -> Path:
    # Prefer direct local_dir mapping; fall back to searching under model_cache_dir.
    direct = model_cache_dir / selected_path
    if direct.is_file():
        return direct
    meta["model_resolve_search_root"] = str(model_cache_dir)
    base = Path(selected_path).name if selected_path else model_filename
    for name in (base, model_filename):
        if not name:
            continue
        hits = sorted(model_cache_dir.rglob(name))
        if hits:
            meta["model_resolve_found"] = [str(p) for p in hits[:5]]
            return hits[0]
    raise RuntimeError(f"download indicated success but checkpoint not found under {model_cache_dir}")

def prepare_model(meta, ckpt_src: Path) -> Path:
    model_dir.mkdir(parents=True, exist_ok=True)
    ckpt_dst = model_dir / ckpt_src.name
    if not ckpt_dst.exists():
        shutil.copy2(ckpt_src, ckpt_dst)
    return ckpt_dst

meta = {
    "timestamp_utc": now_utc(),
    "python": sys.version.split()[0],
    "git_commit": git_commit(),
    "env_vars": {
        "HF_HOME": os.environ.get("HF_HOME", ""),
        "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", ""),
        "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
        "PY_ENV_SOURCE": os.environ.get("PY_ENV_SOURCE", ""),
        "PY_ENV_WARNING": os.environ.get("PY_ENV_WARNING", ""),
    },
    "decision_reason": "Use official toy dataset (facebook/sapiens_toy_dataset), but prune to 1 sample to guarantee exactly 1 train iter when max_epochs=1 and batch_size=1; download smallest pretrain checkpoint for sapiens_0.3b.",
    "reported_python_path": sys.executable,
}

assets = base_assets()
status = "failure"
exit_code = 1
failure_category = "unknown"
model_checkpoint_path = ""

try:
    dataset_prepared = all((dataset_dir / p).exists() for p in ("images", "masks", "normals"))
    existing_ckpt = model_dir / model_filename
    model_prepared = existing_ckpt.is_file()

    cache_has_dataset = bool(list(dataset_cache_dir.glob("*.zip"))) or all(
        (dataset_cache_dir / p).is_dir() for p in ("images", "masks", "normals")
    )
    cache_model_hits = sorted(model_cache_dir.rglob(model_filename)) if model_cache_dir.exists() else []
    cache_has_model = len(cache_model_hits) > 0

    meta["prepared_assets_present"] = {"dataset": dataset_prepared, "model": model_prepared}
    meta["cache_present"] = {"dataset": cache_has_dataset, "model": cache_has_model}

    need_hf = (not dataset_prepared and not cache_has_dataset) or (not model_prepared and not cache_has_model)
    if need_hf:
        ensure_huggingface_hub(meta)

    # Dataset: reuse prepared dataset_dir if present; else reuse cache; else download.
    if not dataset_prepared and not cache_has_dataset:
        print(f"[prepare] downloading dataset: {dataset_id}")
        download_dataset(meta)
        meta["dataset_download_skipped"] = False
    else:
        meta["dataset_download_skipped"] = True
    prepare_dataset(meta)  # always prunes to 1 sample

    # Model: prefer prepared model_dir; else reuse cache; else download.
    if model_prepared:
        ckpt_dst = existing_ckpt
        meta["model_download_skipped"] = True
        meta["model_reuse_mode"] = "reuse_existing_model_dir"
    else:
        if cache_has_model:
            ckpt_src = cache_model_hits[0]
            meta["model_download_skipped"] = True
            meta["model_cache_hit"] = str(ckpt_src)
        else:
            print(f"[prepare] downloading model checkpoint from: {model_id}")
            selected = download_model(meta)
            ckpt_src = locate_model_checkpoint(meta, selected)
            meta["model_download_skipped"] = False
        ckpt_dst = prepare_model(meta, ckpt_src)

    model_checkpoint_path = str(ckpt_dst)

    ds_sha = sha256_dir(dataset_dir)
    m_sha = sha256_file(Path(model_checkpoint_path))
    assets["dataset"] = {
        "path": str(dataset_dir),
        "source": f"hf://datasets/{dataset_id}",
        "version": "main",
        "sha256": ds_sha,
    }
    assets["model"] = {
        "path": str(model_dir),
        "source": f"hf://models/{model_id}",
        "version": "main",
        "sha256": m_sha,
    }

    # If previous results exist and sha256 matches, we can treat this as a full cache hit.
    if results_path.exists():
        try:
            prev = json.loads(results_path.read_text(encoding="utf-8"))
            prev_assets = prev.get("assets") or {}
            prev_ds = (prev_assets.get("dataset") or {}).get("sha256", "")
            prev_m = (prev_assets.get("model") or {}).get("sha256", "")
            meta["previous_results_sha_match"] = {
                "dataset": bool(prev_ds) and prev_ds == ds_sha,
                "model": bool(prev_m) and prev_m == m_sha,
            }
        except Exception as e:
            meta["previous_results_read_error"] = repr(e)

    status = "success"
    exit_code = 0
    failure_category = ""
except subprocess.CalledProcessError as e:
    failure_category = "deps"
    meta["error"] = repr(e)
    print(f"[prepare] ERROR: {meta['error']}")
except Exception as e:
    msg = repr(e)
    meta["error"] = msg
    meta["traceback"] = traceback.format_exc(limit=50)
    if "auth" in msg.lower() or "401" in msg or "403" in msg:
        failure_category = "auth_required"
    elif "install huggingface_hub" in msg.lower():
        failure_category = "deps"
    elif "dataset missing required folders" in msg.lower() or "no image files found" in msg.lower():
        failure_category = "data"
    elif "no matching mask file" in msg.lower() or "no matching normal file" in msg.lower():
        failure_category = "data"
    elif "download model checkpoint" in msg.lower():
        failure_category = "download_failed"
    elif "checkpoint not found" in msg.lower():
        failure_category = "model"
    else:
        failure_category = "download_failed"
    print(f"[prepare] ERROR: {meta['error']}")

payload = {
    "status": status,
    "skip_reason": "not_applicable",
    "exit_code": exit_code,
    "stage": stage,
    "task": task,
    "command": "benchmark_scripts/prepare_assets.sh",
    "timeout_sec": timeout_sec,
    "framework": "unknown",
    "assets": assets,
    "meta": {**meta, "model_checkpoint_path": model_checkpoint_path},
    "failure_category": failure_category,
    "error_excerpt": tail_log(240),
}
write_results(payload)
sys.exit(exit_code)
PY
)

env_prefix=(
  "REPO_ROOT=${REPO_ROOT}"
  "OUT_DIR=${OUT_DIR}"
  "DATASET_ID=${DATASET_ID}"
  "MODEL_ID=${MODEL_ID}"
  "DATASET_CACHE_DIR=${DATASET_CACHE_DIR}"
  "MODEL_CACHE_DIR=${MODEL_CACHE_DIR}"
  "DATASET_DIR=${DATASET_DIR}"
  "MODEL_DIR=${MODEL_DIR}"
  "MODEL_FILENAME=${MODEL_FILENAME}"
  "TIMEOUT_SEC=${TIMEOUT_SEC}"
  "PY_ENV_SOURCE=${PY_ENV_SOURCE}"
  "PY_ENV_WARNING=${PY_ENV_WARNING}"
)

stage_rc=0
if command -v timeout >/dev/null 2>&1; then
  env "${env_prefix[@]}" timeout "${TIMEOUT_SEC}s" "${PY_ENV}" -c "${PREPARE_CODE}" || stage_rc=$?
else
  env "${env_prefix[@]}" "${PY_ENV}" -c "${PREPARE_CODE}" || stage_rc=$?
fi

# If the python process was killed (e.g., timeout), ensure results.json is still valid.
if [[ ${stage_rc} -ne 0 ]]; then
  if [[ -f "${RESULTS_PATH}" ]] && grep -q '"placeholder"[[:space:]]*:[[:space:]]*true' "${RESULTS_PATH}" 2>/dev/null; then
    "${PY_SYS}" - <<'PY' "${RESULTS_PATH}" "${TIMEOUT_SEC}" "${stage_rc}" "${PY_ENV}" "${REPORT_PATH}" "${LOG_PATH}"
import json, os, sys, time
from pathlib import Path

results_path = Path(sys.argv[1])
timeout_sec = int(sys.argv[2])
stage_rc = int(sys.argv[3])
python_exe = sys.argv[4]
report_path = sys.argv[5]
log_path = Path(sys.argv[6])

def tail(path: Path, max_lines: int = 240) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""

failure_category = "timeout" if stage_rc == 124 else "runtime"
payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "prepare",
    "task": "download",
    "command": "benchmark_scripts/prepare_assets.sh",
    "timeout_sec": timeout_sec,
    "framework": "unknown",
    "assets": {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    },
    "meta": {
        "python": "",
        "git_commit": "",
        "env_vars": {
            "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            "PY_ENV": python_exe,
        },
        "decision_reason": "prepare stage did not complete (process terminated before writing results.json)",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "subprocess_returncode": stage_rc,
        "report_path": report_path,
    },
    "failure_category": failure_category,
    "error_excerpt": tail(log_path, 240),
}
results_path.parent.mkdir(parents=True, exist_ok=True)
results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  fi
fi

if [[ ${stage_rc} -eq 0 ]]; then
  exit 0
fi
exit 1
