#!/usr/bin/env bash
set -u

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model checkpoint).

Outputs (always written, even on failure):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Assets are placed under:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Environment:
  HF_TOKEN (optional)           HuggingFace token if required.
  SCIMLOPSBENCH_PYTHON          Python interpreter to use (must have huggingface_hub).
  SCIMLOPSBENCH_REPORT          Override agent report path (used only for metadata).
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="$repo_root/build_output/prepare"
mkdir -p "$out_dir"

log_file="$out_dir/log.txt"
results_json="$out_dir/results.json"
: >"$log_file"
: >"$results_json"
exec > >(tee -a "$log_file") 2>&1

stage="prepare"
task="download"
timeout_sec=1200
framework="unknown"

export BENCHMARK_ASSETS_DIR="$repo_root/benchmark_assets"
export BENCHMARK_CACHE_DIR="$BENCHMARK_ASSETS_DIR/cache"
export BENCHMARK_DATASET_DIR="$BENCHMARK_ASSETS_DIR/dataset"
export BENCHMARK_MODEL_DIR="$BENCHMARK_ASSETS_DIR/model"
mkdir -p "$BENCHMARK_CACHE_DIR" "$BENCHMARK_DATASET_DIR" "$BENCHMARK_MODEL_DIR"

export HOME="$BENCHMARK_CACHE_DIR/home"
export XDG_CACHE_HOME="$BENCHMARK_CACHE_DIR/xdg"
export TMPDIR="$BENCHMARK_CACHE_DIR/tmp"
export HF_HOME="$BENCHMARK_CACHE_DIR/huggingface"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TRANSFORMERS_CACHE="$HF_HOME/transformers"
export TORCH_HOME="$BENCHMARK_CACHE_DIR/torch"
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$TMPDIR" "$HF_HOME" "$HUGGINGFACE_HUB_CACHE" "$TRANSFORMERS_CACHE" "$TORCH_HOME"

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "HF_TOKEN not set. Continuing without authentication."
fi

dataset_repo="Efficient-Large-Model/toy_data"
dataset_repo_type="dataset"
model_repo="Efficient-Large-Model/Sana_600M_1024px"
model_repo_type="model"
model_ckpt_glob="checkpoints/*.pth"

decision_reason="Chosen from docs: docs/sana.md shows 'Download toy dataset' (Efficient-Large-Model/toy_data). Model chosen as smallest listed in docs/model_zoo.md for 1024px training: Efficient-Large-Model/Sana_600M_1024px."

resolve_python() {
  local spec="$1"
  if [[ -z "$spec" ]]; then
    return 0
  fi
  if [[ "$spec" == *" "* ]]; then
    # Do not attempt to parse arguments; require a plain executable path/name.
    echo ""
    return 0
  fi
  if [[ "$spec" == */* ]]; then
    echo "$spec"
    return 0
  fi
  command -v "$spec" 2>/dev/null || echo ""
}

python_candidates=()
add_python_candidate() {
  local cand="$1"
  [[ -z "$cand" ]] && return 0
  local existing
  for existing in "${python_candidates[@]}"; do
    [[ "$existing" == "$cand" ]] && return 0
  done
  python_candidates+=("$cand")
}

# Candidate order: explicit override -> common PATH pythons -> agent report python.
add_python_candidate "$(resolve_python "${SCIMLOPSBENCH_PYTHON:-}")"
add_python_candidate "$(resolve_python "python")"
add_python_candidate "$(resolve_python "python3")"

report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
json_py="$(resolve_python "python3")"
if [[ -z "$json_py" ]]; then
  json_py="$(resolve_python "python")"
fi
if [[ -n "$json_py" && -f "$report_path" ]]; then
  py_from_report="$("$json_py" - <<PY 2>/dev/null || true
import json, pathlib
p = pathlib.Path(${report_path@Q})
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("python_path", "") or "")
except Exception:
    print("")
PY
)"
  add_python_candidate "$(resolve_python "$py_from_report")"
fi

python_exe=""
python_selection_reason=""
for cand in "${python_candidates[@]}"; do
  if [[ ! -x "$cand" ]]; then
    continue
  fi
  if "$cand" -c "import huggingface_hub" >/dev/null 2>&1; then
    python_exe="$cand"
    python_selection_reason="import_ok"
    break
  fi
done

if [[ -z "$python_exe" ]]; then
  # Fallback to first executable candidate (so we can still emit a failure result.json).
  for cand in "${python_candidates[@]}"; do
    if [[ -x "$cand" ]]; then
      python_exe="$cand"
      python_selection_reason="fallback_no_huggingface_hub"
      break
    fi
  done
fi
if [[ -z "$python_exe" ]]; then
  python_exe="python3"
  python_selection_reason="fallback_python3"
fi

echo "[prepare] python_exe=$python_exe ($python_selection_reason)"

REPO_ROOT="$repo_root" \
OUT_DIR="$out_dir" \
DATASET_REPO="$dataset_repo" \
DATASET_REPO_TYPE="$dataset_repo_type" \
MODEL_REPO="$model_repo" \
MODEL_REPO_TYPE="$model_repo_type" \
MODEL_CKPT_GLOB="$model_ckpt_glob" \
DECISION_REASON="$decision_reason" \
"$python_exe" - <<'PY' || true
import json, os, pathlib, subprocess, sys, time, traceback, hashlib, fnmatch
from typing import List, Optional, Tuple

repo_root = pathlib.Path(os.environ["REPO_ROOT"])
out_dir = pathlib.Path(os.environ["OUT_DIR"])
results_path = out_dir / "results.json"
log_path = out_dir / "log.txt"

dataset_repo = os.environ["DATASET_REPO"]
dataset_repo_type = os.environ["DATASET_REPO_TYPE"]
model_repo = os.environ["MODEL_REPO"]
model_repo_type = os.environ["MODEL_REPO_TYPE"]
model_ckpt_glob = os.environ["MODEL_CKPT_GLOB"]
decision_reason = os.environ.get("DECISION_REASON", "")

cache_root = repo_root / "benchmark_assets" / "cache"
dataset_root = repo_root / "benchmark_assets" / "dataset"
model_root = repo_root / "benchmark_assets" / "model"

def git_commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

def tail_lines(path: pathlib.Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:]) if len(lines) > n else "\n".join(lines)
    except Exception:
        return ""

def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def sha256_dir(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    for p in sorted([p for p in path.rglob("*") if p.is_file()]):
        rel = p.relative_to(path).as_posix().encode("utf-8")
        h.update(rel)
        h.update(b"\0")
        h.update(sha256_file(p).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()

def write_results(*, status: str, exit_code: int, failure_category: str, assets: dict, command: str, meta_extra: dict, skip_reason: str = "not_applicable") -> None:
    payload = {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": exit_code,
        "stage": "prepare",
        "task": "download",
        "command": command,
        "timeout_sec": 1200,
        "framework": "unknown",
        "assets": assets,
        "meta": {
            "python": sys.version.split()[0],
            "git_commit": git_commit(),
            "env_vars": {k: os.environ.get(k, "") for k in [
                "HF_TOKEN",
                "HF_HOME",
                "HUGGINGFACE_HUB_CACHE",
                "TRANSFORMERS_CACHE",
                "TORCH_HOME",
                "HOME",
                "XDG_CACHE_HOME",
                "TMPDIR",
            ] if k in os.environ},
            "decision_reason": decision_reason,
            **meta_extra,
        },
        "failure_category": failure_category,
        "error_excerpt": tail_lines(log_path),
    }
    tmp = results_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(results_path)

assets_default = {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
}

try:
    from huggingface_hub import HfApi, hf_hub_download, snapshot_download
except Exception as e:
    write_results(
        status="failure",
        exit_code=1,
        failure_category="deps",
        assets=assets_default,
        command="python -c 'import huggingface_hub'",
        meta_extra={"exception": repr(e)},
    )
    raise SystemExit(1)

def ensure_symlink(link: pathlib.Path, target: pathlib.Path) -> None:
    if link.exists() or link.is_symlink():
        try:
            if link.is_symlink() and link.resolve() == target.resolve():
                return
        except Exception:
            pass
        if link.is_dir() and not link.is_symlink():
            # keep existing; do not delete unexpectedly
            return
        try:
            link.unlink()
        except Exception:
            pass
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=target.is_dir())

start = time.time()
meta_extra = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

# Reuse fast-path if previous results exist and paths still validate.
prev = repo_root / "build_output" / "prepare" / "results.json"
if prev.exists():
    try:
        prev_data = json.loads(prev.read_text(encoding="utf-8"))
        prev_assets = prev_data.get("assets", {}) if isinstance(prev_data, dict) else {}
        ds_path = pathlib.Path(str(prev_assets.get("dataset", {}).get("path", "")))
        md_path = pathlib.Path(str(prev_assets.get("model", {}).get("path", "")))
        if ds_path.exists() and md_path.exists():
            meta_extra["reuse_attempted"] = True
    except Exception:
        pass

token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or None
api = HfApi(token=token)

# 1) Dataset snapshot to cache
dataset_cache_dir = cache_root / "hf_assets" / f"{dataset_repo_type}__{dataset_repo.replace('/', '__')}"
dataset_cache_dir.mkdir(parents=True, exist_ok=True)
dataset_local_dir = dataset_cache_dir / "snapshot"

dataset_version = "main"
dataset_snapshot_ok = False
dataset_error = None
try:
    if not dataset_local_dir.exists() or not any(dataset_local_dir.iterdir()):
        print(f"[prepare] Downloading dataset snapshot: {dataset_repo} -> {dataset_local_dir}")
        snapshot_download(
            repo_id=dataset_repo,
            repo_type=dataset_repo_type,
            revision=dataset_version,
            local_dir=str(dataset_local_dir),
            local_dir_use_symlinks=False,
            token=token,
        )
    dataset_snapshot_ok = True
except Exception as e:
    dataset_error = e
    print(f"[prepare] Dataset download error: {e}")

# 2) Model checkpoint to cache (download only a single .pth under checkpoints/)
model_cache_dir = cache_root / "hf_assets" / f"{model_repo_type}__{model_repo.replace('/', '__')}"
model_cache_dir.mkdir(parents=True, exist_ok=True)
model_local_dir = model_cache_dir / "snapshot"
model_local_dir.mkdir(parents=True, exist_ok=True)

ckpt_relpath = ""
ckpt_local_path: pathlib.Path | None = None
model_version = "main"
model_error = None

def find_cached_ckpt(root: pathlib.Path) -> Tuple[str, Optional[pathlib.Path]]:
    candidates: List[pathlib.Path] = []
    for p in root.rglob("*.pth"):
        if "checkpoints" in p.parts:
            candidates.append(p)
    if not candidates:
        return "", None
    candidates = sorted(candidates, key=lambda p: (len(p.as_posix()), p.as_posix()))
    p = candidates[0]
    try:
        rel = p.relative_to(root).as_posix()
    except Exception:
        rel = p.name
    return rel, p

try:
    # If already downloaded, reuse.
    ckpt_relpath, ckpt_local_path = find_cached_ckpt(model_local_dir)
    if not ckpt_local_path:
        print(f"[prepare] Listing repo files to find a checkpoint: {model_repo}")
        files = api.list_repo_files(repo_id=model_repo, repo_type=model_repo_type)
        ckpt_candidates = [f for f in files if fnmatch.fnmatch(f, model_ckpt_glob)]
        if not ckpt_candidates:
            ckpt_candidates = [f for f in files if f.endswith(".pth")]
        ckpt_candidates = sorted(ckpt_candidates, key=lambda s: (len(s), s))
        if not ckpt_candidates:
            raise RuntimeError(f"no .pth found in repo {model_repo}")
        ckpt_relpath = ckpt_candidates[0]
        print(f"[prepare] Downloading model checkpoint: {model_repo}:{ckpt_relpath} -> {model_local_dir}")
        downloaded = hf_hub_download(
            repo_id=model_repo,
            repo_type=model_repo_type,
            filename=ckpt_relpath,
            revision=model_version,
            local_dir=str(model_local_dir),
            local_dir_use_symlinks=False,
            token=token,
        )
        ckpt_local_path = pathlib.Path(downloaded)
except Exception as e:
    model_error = e
    print(f"[prepare] Model download error: {e}")

# Resolve final dataset/model paths under benchmark_assets/
dataset_link = dataset_root / "toy_data"
model_link = model_root / "sana_600m_1024px"

if dataset_snapshot_ok and dataset_local_dir.exists():
    try:
        ensure_symlink(dataset_link, dataset_local_dir)
    except Exception as e:
        dataset_snapshot_ok = False
        dataset_error = e

model_ok = ckpt_local_path is not None and ckpt_local_path.exists()
if model_ok:
    try:
        ensure_symlink(model_link, model_local_dir)
    except Exception as e:
        model_ok = False
        model_error = e

if not dataset_snapshot_ok:
    failure_cat = "download_failed"
    if dataset_error and ("401" in str(dataset_error) or "403" in str(dataset_error)):
        failure_cat = "auth_required"
    write_results(
        status="failure",
        exit_code=1,
        failure_category=failure_cat,
        assets=assets_default,
        command="huggingface_hub.snapshot_download(...)",
        meta_extra={"dataset_error": repr(dataset_error)},
    )
    raise SystemExit(1)

if not model_ok:
    # Distinguish between downloader failures vs "download looked ok but cannot locate/verify artifacts".
    if model_error and ("401" in str(model_error) or "403" in str(model_error)):
        failure_cat = "auth_required"
    elif model_error is not None:
        failure_cat = "download_failed"
    else:
        failure_cat = "model"
    write_results(
        status="failure",
        exit_code=1,
        failure_category=failure_cat,
        assets=assets_default,
        command="huggingface_hub.hf_hub_download(...)",
        meta_extra={
            "model_error": repr(model_error),
            "model_cache_root": str(model_local_dir),
            "checkpoint_relpath": ckpt_relpath,
            "checkpoint_path": str(ckpt_local_path) if ckpt_local_path is not None else "",
        },
    )
    raise SystemExit(1)

# Compute SHA256 for offline reuse checks (directory for dataset, file for checkpoint).
dataset_sha = sha256_dir(dataset_local_dir)
model_sha = sha256_file(ckpt_local_path)

assets = {
    "dataset": {
        "path": str(dataset_link.resolve()),
        "source": f"hf://{dataset_repo} (repo_type={dataset_repo_type})",
        "version": dataset_version,
        "sha256": dataset_sha,
    },
    "model": {
        "path": str(model_link.resolve()),
        "source": f"hf://{model_repo}:{ckpt_relpath} (repo_type={model_repo_type})",
        "version": model_version,
        "sha256": model_sha,
    },
}

meta_extra.update(
    {
        "dataset_cache_dir": str(dataset_local_dir),
        "model_cache_dir": str(model_local_dir),
        "model_checkpoint_file": str(ckpt_local_path),
        "duration_sec": round(time.time() - start, 3),
    }
)

write_results(
    status="success",
    exit_code=0,
    failure_category="",
    assets=assets,
    command="benchmark_scripts/prepare_assets.sh",
    meta_extra=meta_extra,
)
PY

exit_code="$("$python_exe" - "$results_json" <<'PY' 2>/dev/null || echo 1
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(int(d.get("exit_code", 1)))
except Exception:
    print(1)
PY
)"
exit "$exit_code"
