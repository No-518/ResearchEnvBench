#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + model snapshots) for this repository.

Outputs (always written):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Assets:
  benchmark_assets/cache/         # download cache (HF_HOME points here)
  benchmark_assets/dataset/       # prepared dataset inputs
  benchmark_assets/model/         # resolved model directory (symlinks to cache snapshots)

Options:
  --report-path <path>            Agent report.json path (default: /opt/scimlopsbench/report.json)
  --python <path>                 Explicit python to use (overrides report.json)
  --timeout-sec <int>             Default: 1200
EOF
}

report_path=""
python_bin=""
timeout_sec=1200

while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-path) report_path="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OUT_DIR="$REPO_ROOT/build_output/prepare"
LOG_PATH="$OUT_DIR/log.txt"
RESULTS_JSON="$OUT_DIR/results.json"

ASSETS_ROOT="$REPO_ROOT/benchmark_assets"
CACHE_DIR="$ASSETS_ROOT/cache"
DATASET_DIR="$ASSETS_ROOT/dataset"
MODEL_DIR="$ASSETS_ROOT/model"
HF_HOME_DIR="$CACHE_DIR/huggingface"

mkdir -p "$OUT_DIR" "$CACHE_DIR" "$DATASET_DIR" "$MODEL_DIR"
: >"$LOG_PATH"
exec > >(tee -a "$LOG_PATH") 2>&1

status="failure"
skip_reason="unknown"
failure_category="unknown"
decision_reason=""
command=""
wrote_results=0

dataset_path=""
dataset_source=""
dataset_version=""
dataset_sha256=""

model_root_path=""
model_source=""
model_version=""
model_sha256=""

git_commit="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"

write_results() {
  local stage_exit_code="$1"
  if [[ "$wrote_results" -eq 1 ]]; then
    return 0
  fi
  wrote_results=1

  local error_excerpt
  error_excerpt="$(tail -n 220 "$LOG_PATH" 2>/dev/null || true)"

  python_cmd_for_json="$(command -v python 2>/dev/null || true)"
  if [[ -z "$python_cmd_for_json" ]]; then
    python_cmd_for_json="$(command -v python3 2>/dev/null || true)"
  fi

  if [[ -n "$python_cmd_for_json" ]]; then
    STAGE_STATUS="$status" SKIP_REASON="$skip_reason" FAILURE_CATEGORY="$failure_category" \
    TIMEOUT_SEC="$timeout_sec" COMMAND_STR="$command" GIT_COMMIT="$git_commit" DECISION_REASON="$decision_reason" \
    OUT_DIR="$OUT_DIR" PY_USED="$PY_USED" \
    DATASET_PATH="$dataset_path" DATASET_SOURCE="$dataset_source" DATASET_VERSION="$dataset_version" DATASET_SHA256="$dataset_sha256" \
    MODEL_PATH="$model_root_path" MODEL_SOURCE="$model_source" MODEL_VERSION="$model_version" MODEL_SHA256="$model_sha256" \
    ERROR_EXCERPT="$error_excerpt" \
    "$python_cmd_for_json" - <<'PY'
import json
import os
from datetime import datetime, timezone

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

status = os.environ.get("STAGE_STATUS", "failure")
exit_code = 0 if status in ("success", "skipped") else 1

payload = {
    "status": status,
    "skip_reason": os.environ.get("SKIP_REASON", "unknown"),
    "exit_code": exit_code,
    "stage": "prepare",
    "task": "download",
    "command": os.environ.get("COMMAND_STR", ""),
    "timeout_sec": int(os.environ.get("TIMEOUT_SEC", "0") or 0),
    "framework": "pytorch",
    "assets": {
        "dataset": {
            "path": os.environ.get("DATASET_PATH", ""),
            "source": os.environ.get("DATASET_SOURCE", ""),
            "version": os.environ.get("DATASET_VERSION", ""),
            "sha256": os.environ.get("DATASET_SHA256", ""),
        },
        "model": {
            "path": os.environ.get("MODEL_PATH", ""),
            "source": os.environ.get("MODEL_SOURCE", ""),
            "version": os.environ.get("MODEL_VERSION", ""),
            "sha256": os.environ.get("MODEL_SHA256", ""),
        },
    },
    "meta": {
        "python": os.environ.get("PY_USED", ""),
        "git_commit": os.environ.get("GIT_COMMIT") or None,
        "env_vars": {
            "HF_HOME": os.environ.get("HF_HOME", ""),
            "HUGGINGFACE_HUB_CACHE": os.environ.get("HUGGINGFACE_HUB_CACHE", ""),
        },
        "decision_reason": os.environ.get("DECISION_REASON", ""),
        "timestamp_utc": utc_now(),
    },
    "failure_category": os.environ.get("FAILURE_CATEGORY", "unknown"),
    "error_excerpt": os.environ.get("ERROR_EXCERPT", "")[-20000:],
}

out_path = os.path.join(os.environ.get("OUT_DIR", "build_output/prepare"), "results.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  else
    cat >"$RESULTS_JSON" <<EOF
{
  "status": "$status",
  "skip_reason": "$skip_reason",
  "exit_code": $stage_exit_code,
  "stage": "prepare",
  "task": "download",
  "command": "",
  "timeout_sec": $timeout_sec,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "$dataset_path", "source": "$dataset_source", "version": "$dataset_version", "sha256": "$dataset_sha256"},
    "model": {"path": "$model_root_path", "source": "$model_source", "version": "$model_version", "sha256": "$model_sha256"}
  },
  "meta": {"python": "", "git_commit": null, "env_vars": {}, "decision_reason": ""},
  "failure_category": "$failure_category",
  "error_excerpt": ""
}
EOF
  fi
}

on_exit() {
  local rc="$1"
  if [[ "$wrote_results" -eq 0 ]]; then
    if [[ "$rc" -eq 0 ]]; then
      status="${status:-success}"
    else
      status="${status:-failure}"
    fi
    write_results "$rc"
  fi
}
trap 'on_exit $?' EXIT

echo "[prepare] repo_root=$REPO_ROOT"
echo "[prepare] hf_home=$HF_HOME_DIR"

# ---- Resolve python (must be from report unless --python provided) ----
PY_USED=""
if [[ -n "$python_bin" ]]; then
  PY_USED="$python_bin"
else
  set +e
  if [[ -n "$report_path" ]]; then
    PY_USED="$(python "$REPO_ROOT/benchmark_scripts/runner.py" resolve-python --report-path "$report_path" 2>/dev/null)"
  else
    PY_USED="$(python "$REPO_ROOT/benchmark_scripts/runner.py" resolve-python 2>/dev/null)"
  fi
  set -e
fi

if [[ -z "$PY_USED" ]]; then
  status="failure"
  failure_category="missing_report"
  decision_reason="Could not resolve python via report.json (and --python not provided)."
  command="python benchmark_scripts/runner.py resolve-python"
  echo "[prepare] ERROR: $decision_reason" >&2
  exit 1
fi

echo "[prepare] using python: $PY_USED"
export HF_HOME="$HF_HOME_DIR"
export HUGGINGFACE_HUB_CACHE="$HF_HOME_DIR/hub"
export HF_HUB_DISABLE_TELEMETRY=1
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if ! "$PY_USED" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  status="failure"
  failure_category="deps"
  decision_reason="Resolved python is not runnable: $PY_USED"
  command="$PY_USED -c 'import sys; print(sys.executable)'"
  echo "[prepare] ERROR: $decision_reason" >&2
  exit 1
fi

# ---- Dataset preparation (use official demo sample) ----
dataset_src_candidate=""
if [[ -f "$REPO_ROOT/demo/sample_text.jpg" ]]; then
  dataset_src_candidate="$REPO_ROOT/demo/sample_text.jpg"
elif [[ -f "$REPO_ROOT/demo/sample.pdf" ]]; then
  dataset_src_candidate="$REPO_ROOT/demo/sample.pdf"
fi

if [[ -z "$dataset_src_candidate" ]]; then
  status="failure"
  failure_category="data"
  decision_reason="No bundled demo dataset found (expected demo/sample_text.jpg or demo/sample.pdf)."
  command="cp demo/<sample> benchmark_assets/dataset/"
  echo "[prepare] ERROR: $decision_reason" >&2
  exit 1
fi

dataset_basename="$(basename "$dataset_src_candidate")"
dataset_path="$DATASET_DIR/$dataset_basename"
dataset_source="repo:${dataset_src_candidate#"$REPO_ROOT/"}"
dataset_version=""

echo "[prepare] dataset source: $dataset_source"

if command -v sha256sum >/dev/null 2>&1; then
  src_sha="$(sha256sum "$dataset_src_candidate" | awk '{print $1}')"
else
  src_sha="$("$PY_USED" - <<PY
import hashlib, pathlib
p=pathlib.Path(r"""$dataset_src_candidate""")
h=hashlib.sha256()
with p.open("rb") as f:
    for chunk in iter(lambda: f.read(1024*1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
)"
fi

if [[ -f "$dataset_path" ]]; then
  if command -v sha256sum >/dev/null 2>&1; then
    dst_sha="$(sha256sum "$dataset_path" | awk '{print $1}')"
  else
    dst_sha="$("$PY_USED" - <<PY
import hashlib, pathlib
p=pathlib.Path(r"""$dataset_path""")
h=hashlib.sha256()
with p.open("rb") as f:
    for chunk in iter(lambda: f.read(1024*1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
)"
  fi
  if [[ "$dst_sha" == "$src_sha" ]]; then
    echo "[prepare] dataset already prepared (sha256 match)"
  else
    cp -f "$dataset_src_candidate" "$dataset_path"
  fi
else
  cp -f "$dataset_src_candidate" "$dataset_path"
fi
dataset_sha256="$src_sha"

# ---- Determine required model repos from repo source (DEFAULT_CONFIGS) ----
model_repos_json="$OUT_DIR/model_repos.json"
set +e
REPO_ROOT="$REPO_ROOT" "$PY_USED" - <<'PY' >"$model_repos_json"
import ast
import json
import os
from pathlib import Path

repo_root = Path(os.environ["REPO_ROOT"]).resolve()
configs_init = repo_root / "src" / "yomitoku" / "configs" / "__init__.py"
configs_dir = repo_root / "src" / "yomitoku" / "configs"

def load_default_config_names() -> list[str]:
    tree = ast.parse(configs_init.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIGS" for t in node.targets):
                if isinstance(node.value, ast.List):
                    names = []
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Name):
                            names.append(elt.id)
                    return names
    return []

def find_hf_repo_for_class(class_name: str) -> str | None:
    for py in sorted(configs_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "hf_hub_repo":
                        if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                            return stmt.value.value
                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "hf_hub_repo":
                        if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                            return stmt.value.value
    return None

names = load_default_config_names()
repos: list[str] = []
missing: list[str] = []
for name in names:
    repo = find_hf_repo_for_class(name)
    if repo:
        repos.append(repo)
    else:
        missing.append(name)

print(json.dumps({"default_config_classes": names, "repos": repos, "missing": missing}, ensure_ascii=False, indent=2))
PY
detect_rc=$?
set -e

if [[ "$detect_rc" -ne 0 ]]; then
  status="failure"
  failure_category="deps"
  decision_reason="Failed to detect model repos from src/yomitoku/configs."
  command="$PY_USED (AST parse src/yomitoku/configs)"
  echo "[prepare] ERROR: $decision_reason" >&2
  exit 1
fi

model_repos="$("$PY_USED" - <<PY
import json
from pathlib import Path
obj=json.loads(Path(r"""$model_repos_json""").read_text(encoding="utf-8"))
repos=obj.get("repos", [])
missing=obj.get("missing", [])
if missing:
    raise SystemExit(f"missing hf_hub_repo for: {missing}")
print("\\n".join(repos))
PY
)"

echo "[prepare] model repos:"
echo "$model_repos" | sed 's/^/  - /'

# ---- Offline-first reuse: if resolved model dir already exists, skip downloads ----
model_root_path="$MODEL_DIR/yomitoku_default_models"
model_source="huggingface_hub:snapshot_download"
decision_reason="Using hf_hub_repo values from src/yomitoku/configs DEFAULT_CONFIGS; dataset from demo sample."

reuse_check_json="$OUT_DIR/model_reuse_check.json"
set +e
MODEL_REPOS="$model_repos" MODEL_ROOT="$model_root_path" "$PY_USED" - <<'PY' >"$reuse_check_json"
import hashlib
import json
import os
from pathlib import Path

repos = [r.strip() for r in os.environ.get("MODEL_REPOS", "").splitlines() if r.strip()]
root = Path(os.environ.get("MODEL_ROOT", ""))

def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

ok = True
items = []
for repo_id in repos:
    name = repo_id.split("/")[-1]
    p_dir = root / name
    p_ptr = root / f"{name}.pointer.txt"
    if p_dir.is_symlink():
        target = p_dir.resolve()
        commit = target.name
        items.append((repo_id, commit))
    elif p_dir.exists() and p_dir.is_dir():
        items.append((repo_id, ""))
    elif p_ptr.exists():
        try:
            target = Path(p_ptr.read_text(encoding="utf-8").strip())
            commit = target.name
        except Exception:
            commit = ""
        items.append((repo_id, commit))
    else:
        ok = False

version = ";".join([f"{r}@{c}" for r, c in items]) if items else ""
out = {"reusable": bool(ok and items), "model_root": str(root), "version": version, "sha256": sha256_str(version) if version else "", "items": items}
print(json.dumps(out, ensure_ascii=False, indent=2))
PY
reuse_rc=$?
set -e

if [[ "$reuse_rc" -eq 0 ]]; then
  reusable="$("$PY_USED" - <<PY
import json
from pathlib import Path
obj=json.loads(Path(r"""$reuse_check_json""").read_text(encoding="utf-8"))
print("1" if obj.get("reusable") else "0")
PY
)"
  if [[ "$reusable" == "1" ]]; then
    echo "[prepare] Reusing existing model directory: $model_root_path"
    model_version="$("$PY_USED" - <<PY
import json
from pathlib import Path
obj=json.loads(Path(r"""$reuse_check_json""").read_text(encoding="utf-8"))
print(obj.get("version",""))
PY
)"
    model_sha256="$("$PY_USED" - <<PY
import json
from pathlib import Path
obj=json.loads(Path(r"""$reuse_check_json""").read_text(encoding="utf-8"))
print(obj.get("sha256",""))
PY
)"
    "$PY_USED" - <<PY >"$OUT_DIR/model_resolved.json"
import json
from pathlib import Path
obj=json.loads(Path(r"""$reuse_check_json""").read_text(encoding="utf-8"))
Path(r"""$OUT_DIR/model_resolved.json""").write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY
    status="success"
    failure_category="unknown"
    write_results 0
    exit 0
  fi
fi

# ---- Download models into benchmark_assets/cache (HF_HOME) ----
download_manifest="$OUT_DIR/model_download_manifest.json"
command="$PY_USED -c 'huggingface_hub.snapshot_download(...)'"
decision_reason="Using hf_hub_repo values from src/yomitoku/configs DEFAULT_CONFIGS; dataset from demo sample."

set +e
MODEL_REPOS="$model_repos" CACHE_DIR="$CACHE_DIR" HF_HOME_DIR="$HF_HOME_DIR" "$PY_USED" - <<'PY' >"$download_manifest"
import hashlib
import json
import os
import sys
from pathlib import Path

repos = [r.strip() for r in os.environ.get("MODEL_REPOS", "").splitlines() if r.strip()]
cache_root = Path(os.environ["CACHE_DIR"])
hf_home = Path(os.environ["HF_HOME_DIR"])
hub_cache = hf_home / "hub"

payload = {"models": [], "errors": []}

try:
    from huggingface_hub import snapshot_download
except Exception as e:
    payload["errors"].append({"type": "deps", "error": f"failed to import huggingface_hub: {e}"})
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(2)

def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

for repo_id in repos:
    entry = {"repo_id": repo_id, "revision": "main", "snapshot_path": None, "commit": None, "sha256": None, "status": "failure", "error": None}
    try:
        snap = snapshot_download(repo_id=repo_id, revision="main", cache_dir=str(hub_cache))
        entry["snapshot_path"] = snap
        entry["commit"] = Path(snap).name
        entry["sha256"] = sha256_str(f"{repo_id}@{entry['commit']}")
        entry["status"] = "success"
    except Exception as e:
        # Offline reuse: retry local_files_only=True
        try:
            snap = snapshot_download(repo_id=repo_id, revision="main", cache_dir=str(hub_cache), local_files_only=True)
            entry["snapshot_path"] = snap
            entry["commit"] = Path(snap).name
            entry["sha256"] = sha256_str(f"{repo_id}@{entry['commit']}")
            entry["status"] = "success"
            entry["error"] = f"network failed; used local_files_only cache: {e}"
        except Exception as e2:
            entry["error"] = f"{e2}"
    payload["models"].append(entry)

print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
dl_rc=$?
set -e

if [[ "$dl_rc" -ne 0 ]]; then
  status="failure"
  if grep -qi "auth" "$LOG_PATH" 2>/dev/null; then
    failure_category="auth_required"
  else
    failure_category="download_failed"
  fi
  echo "[prepare] ERROR: model download failed (rc=$dl_rc)" >&2
  exit 1
fi

# Verify download manifest and paths exist.
if ! "$PY_USED" -c 'import json,sys; json.load(open(sys.argv[1],"r",encoding="utf-8"))' "$download_manifest" >/dev/null 2>&1; then
  status="failure"
  failure_category="invalid_json"
  echo "[prepare] ERROR: download manifest is invalid JSON" >&2
  exit 1
fi

# Create resolved model directory with symlinks (no assumptions about HF cache layout; use snapshot_download paths).
model_root_path="$MODEL_DIR/yomitoku_default_models"
mkdir -p "$model_root_path"

set +e
"$PY_USED" - <<PY
import json
import os
from pathlib import Path

manifest = json.loads(Path(r"""$download_manifest""").read_text(encoding="utf-8"))
root = Path(r"""$model_root_path""")
root.mkdir(parents=True, exist_ok=True)

failed = []
pairs = []
for m in manifest.get("models", []):
    if m.get("status") != "success":
        failed.append(m)
        continue
    snap = Path(str(m.get("snapshot_path") or ""))
    if not snap.exists():
        failed.append({**m, "error": f"snapshot_path does not exist: {snap}"})
        continue
    name = m["repo_id"].split("/")[-1]
    link = root / name
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(snap)
    except Exception:
        # fallback to writing a pointer file if symlinks are not allowed
        link = root / f"{name}.pointer.txt"
        link.write_text(str(snap) + "\n", encoding="utf-8")
    pairs.append((m["repo_id"], m.get("commit") or "", m.get("sha256") or ""))

if failed:
    raise SystemExit("model download succeeded but resolved directory could not be verified: " + json.dumps(failed, ensure_ascii=False))

version = ";".join([f"{r}@{c}" for r,c,_ in pairs])
sha = __import__("hashlib").sha256(version.encode("utf-8")).hexdigest()
out = {"model_root": str(root), "version": version, "sha256": sha, "models": pairs}
Path(r"""$OUT_DIR/model_resolved.json""").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
link_rc=$?
set -e

if [[ "$link_rc" -ne 0 ]]; then
  status="failure"
  failure_category="model"
  echo "[prepare] ERROR: model path resolution failed" >&2
  exit 1
fi

model_resolved_json="$OUT_DIR/model_resolved.json"
model_version="$("$PY_USED" - <<PY
import json
from pathlib import Path
obj=json.loads(Path(r"""$model_resolved_json""").read_text(encoding="utf-8"))
print(obj.get("version",""))
PY
)"
model_sha256="$("$PY_USED" - <<PY
import json
from pathlib import Path
obj=json.loads(Path(r"""$model_resolved_json""").read_text(encoding="utf-8"))
print(obj.get("sha256",""))
PY
)"

if [[ ! -d "$model_root_path" ]]; then
  status="failure"
  failure_category="model"
  echo "[prepare] ERROR: resolved model directory does not exist: $model_root_path" >&2
  exit 1
fi

status="success"
failure_category="unknown"
write_results 0
exit 0
