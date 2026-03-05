#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

STAGE_DIR="build_output/prepare"
LOG_FILE="$STAGE_DIR/log.txt"
RESULTS_JSON="$STAGE_DIR/results.json"
MANIFEST_JSON="benchmark_assets/manifest.json"

mkdir -p "$STAGE_DIR"
mkdir -p benchmark_assets/cache benchmark_assets/dataset benchmark_assets/model

echo "[prepare] repo_root=$REPO_ROOT" >"$LOG_FILE"

dataset_url="https://www.cs.toronto.edu/~kriz/cifar-10-python.tar.gz"
dataset_cache="benchmark_assets/cache/cifar-10-python.tar.gz"
dataset_dir="benchmark_assets/dataset/cifar10"

model_url="https://download.pytorch.org/models/squeezenet1_0-b66bff10.pth"
model_cache="benchmark_assets/cache/squeezenet1_0-b66bff10.pth"
model_dir="benchmark_assets/model/squeezenet1_0"
model_file="$model_dir/squeezenet1_0-b66bff10.pth"

dataset_sha=""
model_sha=""

download() {
  local url="$1"
  local dst="$2"
  if [[ -f "$dst" ]]; then
    echo "[prepare] cache hit: $dst" >>"$LOG_FILE"
    return 0
  fi

  echo "[prepare] downloading: $url -> $dst" >>"$LOG_FILE"
  mkdir -p "$(dirname "$dst")"
  if command -v curl >/dev/null 2>&1; then
    if ! curl -L --fail --retry 3 --connect-timeout 10 -o "${dst}.tmp" "$url" >>"$LOG_FILE" 2>&1; then
      rm -f "${dst}.tmp" || true
      return 1
    fi
  elif command -v wget >/dev/null 2>&1; then
    if ! wget -O "${dst}.tmp" "$url" >>"$LOG_FILE" 2>&1; then
      rm -f "${dst}.tmp" || true
      return 1
    fi
  else
    echo "[prepare] neither curl nor wget found" >>"$LOG_FILE"
    return 2
  fi
  mv "${dst}.tmp" "$dst"
  return 0
}

sha256_file() {
  python3 - "$1" <<'PY'
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
h = hashlib.sha256()
with p.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
}

write_results() {
  python3 - <<'PY'
import json, os, pathlib, time, subprocess, sys

repo_root = pathlib.Path(os.environ["REPO_ROOT"])
stage_dir = repo_root / "build_output" / "prepare"
results_path = stage_dir / "results.json"
manifest_path = repo_root / "benchmark_assets" / "manifest.json"
log_file = stage_dir / "log.txt"

def git_commit() -> str:
    try:
        cp = subprocess.run(["git","rev-parse","HEAD"], cwd=repo_root, text=True, capture_output=True, check=False)
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""

def tail(max_lines: int = 220) -> str:
    try:
        txt = log_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return "\n".join(txt.splitlines()[-max_lines:]).strip()

assets = json.loads(os.environ.get("ASSETS_JSON","{}")) if os.environ.get("ASSETS_JSON") else {}

payload = {
  "status": os.environ.get("STATUS","failure"),
  "skip_reason": os.environ.get("SKIP_REASON","not_applicable"),
  "exit_code": int(os.environ.get("EXIT_CODE","1")),
  "stage": "prepare",
  "task": "download",
  "command": os.environ.get("COMMAND","benchmark_scripts/prepare_assets.sh"),
  "timeout_sec": int(os.environ.get("TIMEOUT_SEC","1200")),
  "framework": "unknown",
  "assets": assets or {
    "dataset": {"path":"", "source":"", "version":"", "sha256":""},
    "model": {"path":"", "source":"", "version":"", "sha256":""},
  },
  "meta": {
    "python": sys.executable,
    "git_commit": git_commit(),
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "decision_reason": os.environ.get("DECISION_REASON",""),
    "offline_reuse_ok": True,
    "env_vars": {k: os.environ.get(k, "") for k in sorted(os.environ) if k.startswith("SCIMLOPSBENCH_")},
  },
  "failure_category": os.environ.get("FAILURE_CATEGORY","unknown"),
  "error_excerpt": os.environ.get("ERROR_EXCERPT","") or tail(),
}

results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

if os.environ.get("WRITE_MANIFEST","0") == "1":
    manifest = {
      "assets": payload["assets"],
      "meta": payload["meta"],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

status="success"
failure_category="not_applicable"
error_excerpt=""

dataset_downloaded=0
model_downloaded=0

dl_rc=0
if ! download "$dataset_url" "$dataset_cache"; then
  dl_rc=$?
  if [[ -f "$dataset_cache" ]]; then
    echo "[prepare] dataset download failed, using existing cache (offline reuse)" >>"$LOG_FILE"
  else
    status="failure"
    failure_category="$([[ "$dl_rc" -eq 2 ]] && echo deps || echo download_failed)"
    error_excerpt="dataset download failed and cache missing"
  fi
else
  dataset_downloaded=1
fi

dl_rc=0
if ! download "$model_url" "$model_cache"; then
  dl_rc=$?
  if [[ -f "$model_cache" ]]; then
    echo "[prepare] model download failed, using existing cache (offline reuse)" >>"$LOG_FILE"
  else
    status="failure"
    failure_category="$([[ "$dl_rc" -eq 2 ]] && echo deps || echo download_failed)"
    error_excerpt="model download failed and cache missing"
  fi
else
  model_downloaded=1
fi

if [[ "$status" == "success" ]]; then
  dataset_sha="$(sha256_file "$dataset_cache")"
  model_sha="$(sha256_file "$model_cache")"

  mkdir -p "$dataset_dir"
  if [[ ! -d "$dataset_dir/cifar-10-batches-py" ]]; then
    echo "[prepare] extracting dataset into $dataset_dir" >>"$LOG_FILE"
    if ! tar -xzf "$dataset_cache" -C "$dataset_dir" >>"$LOG_FILE" 2>&1; then
      status="failure"
      failure_category="data"
      error_excerpt="failed to extract CIFAR10 tarball"
    fi
  else
    echo "[prepare] dataset already extracted: $dataset_dir/cifar-10-batches-py" >>"$LOG_FILE"
  fi

  mkdir -p "$model_dir"
  if [[ ! -f "$model_file" ]]; then
    echo "[prepare] staging model weights into $model_file" >>"$LOG_FILE"
    cp -f "$model_cache" "$model_file"
  fi

  if [[ ! -d "$model_dir" ]]; then
    status="failure"
    failure_category="model"
    error_excerpt="model directory missing after download"
  fi
fi

assets_json="$(python3 - <<PY
import json, os
print(json.dumps({
  "dataset": {
    "path": os.path.join("$REPO_ROOT", "$dataset_dir"),
    "source": "$dataset_url",
    "version": "cifar10-python",
    "sha256": "" if "$status" != "success" else "$dataset_sha",
  },
  "model": {
    "path": os.path.join("$REPO_ROOT", "$model_dir"),
    "source": "$model_url",
    "version": "squeezenet1_0-b66bff10",
    "sha256": "" if "$status" != "success" else "$model_sha",
  },
}))
PY
)"

{
  echo "[prepare] dataset_cache=$dataset_cache downloaded=$dataset_downloaded"
  echo "[prepare] model_cache=$model_cache downloaded=$model_downloaded"
  echo "[prepare] status=$status failure_category=$failure_category"
} >>"$LOG_FILE"

DECISION_REASON="Dataset is CIFAR10 (used by train_ddp.py); model is a tiny public torchvision weight file (no auth)." \
  STATUS="$status" EXIT_CODE="$([[ "$status" == "success" ]] && echo 0 || echo 1)" \
  FAILURE_CATEGORY="$failure_category" ERROR_EXCERPT="$error_excerpt" \
  ASSETS_JSON="$assets_json" REPO_ROOT="$REPO_ROOT" WRITE_MANIFEST="$([[ "$status" == "success" ]] && echo 1 || echo 0)" \
  write_results

if [[ "$status" == "success" ]]; then
  exit 0
fi
exit 1
