#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare benchmark assets (dataset + minimal model weights) into benchmark_assets/.

Outputs (always written, even on failure):
  build_output/prepare/log.txt
  build_output/prepare/results.json

Directories created/used:
  benchmark_assets/cache/
  benchmark_assets/dataset/
  benchmark_assets/model/

Options:
  --python <cmd>       Python command or path to use (highest priority)
  --repo <path>        Repo root (default: auto-detect)
EOF
}

python_arg=""
repo=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_arg="${2:-}"; shift 2 ;;
    --repo) repo="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
repo="${repo:-$REPO_ROOT}"

OUT_DIR="$REPO_ROOT/build_output/prepare"
mkdir -p "$OUT_DIR"
LOG_FILE="$OUT_DIR/log.txt"
RESULTS_JSON="$OUT_DIR/results.json"

exec > >(tee "$LOG_FILE") 2>&1

cd "$repo"

stage_status="success"
failure_category=""
skip_reason="unknown"
exit_code=0
decision_reason="Using docker/scripts/download_model.py for model; using a minimal prompts text file for dataset."

# Avoid writing __pycache__ into the repository.
export PYTHONDONTWRITEBYTECODE=1

resolve_python() {
  if [[ -n "$python_arg" ]]; then
    # shellcheck disable=SC2206
    PY_CMD=($python_arg)
    PY_SOURCE="cli"
    return 0
  fi
  if [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
    # shellcheck disable=SC2206
    PY_CMD=(${SCIMLOPSBENCH_PYTHON})
    PY_SOURCE="env"
    return 0
  fi

  local report_path="${SCIMLOPSBENCH_REPORT:-/opt/scimlopsbench/report.json}"
  if [[ ! -f "$report_path" ]]; then
    PY_CMD=(python)
    PY_SOURCE="missing_report"
    return 1
  fi

  local sys_py
  if command -v python3 >/dev/null 2>&1; then
    sys_py="python3"
  elif command -v python >/dev/null 2>&1; then
    sys_py="python"
  else
    PY_CMD=(python)
    PY_SOURCE="missing_report"
    return 1
  fi

  local resolved
  if ! resolved="$("$sys_py" - <<PY
import json, os, sys
path = os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"
try:
    data = json.load(open(path, "r", encoding="utf-8"))
except Exception as e:
    print(f"__ERROR__:{e}")
    sys.exit(2)
pp = data.get("python_path")
if not isinstance(pp, str) or not pp.strip():
    print("__ERROR__:missing python_path")
    sys.exit(3)
print(pp)
PY
)"; then
    PY_CMD=(python)
    PY_SOURCE="missing_report"
    return 1
  fi
  if [[ "$resolved" == __ERROR__:* ]]; then
    PY_CMD=(python)
    PY_SOURCE="missing_report"
    return 1
  fi

  # shellcheck disable=SC2206
  PY_CMD=($resolved)
  PY_SOURCE="report"
  return 0
}

PY_CMD=(python)
PY_SOURCE=""
if ! resolve_python; then
  echo "Failed to resolve python via report/env/--python" >&2
  stage_status="failure"
  failure_category="missing_report"
  exit_code=1
fi

python_cmd_str="${PY_CMD[*]}"
echo "Repo: $repo"
echo "Python cmd: $python_cmd_str (source=$PY_SOURCE)"

# Choose a best-effort python for stdlib-only helpers (hashing / JSON writing).
WRITER_PY_CMD=("${PY_CMD[@]}")
if ! "${WRITER_PY_CMD[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    WRITER_PY_CMD=(python3)
  elif command -v python >/dev/null 2>&1; then
    WRITER_PY_CMD=(python)
  fi
fi

# Asset locations
CACHE_ROOT="$REPO_ROOT/benchmark_assets/cache"
DATASET_DIR="$REPO_ROOT/benchmark_assets/dataset"
MODEL_DIR="$REPO_ROOT/benchmark_assets/model"

DATASET_CACHE_DIR="$CACHE_ROOT/dataset"
MODEL_CACHE_DIR="$CACHE_ROOT/model/v1_0"
MODEL_OUT_DIR="$MODEL_DIR/v1_0"

mkdir -p "$DATASET_CACHE_DIR" "$MODEL_CACHE_DIR" "$DATASET_DIR" "$MODEL_OUT_DIR"

DATASET_FILE_CACHE="$DATASET_CACHE_DIR/prompts.txt"
DATASET_FILE="$DATASET_DIR/prompts.txt"

MODEL_FILE_CACHE="$MODEL_CACHE_DIR/kokoro-v1_0.pth"
MODEL_CONFIG_CACHE="$MODEL_CACHE_DIR/config.json"
MODEL_FILE="$MODEL_OUT_DIR/kokoro-v1_0.pth"
MODEL_CONFIG="$MODEL_OUT_DIR/config.json"

dataset_source="generated:benchmark_assets/cache/dataset/prompts.txt"
dataset_version="v1"
model_source="https://github.com/remsky/Kokoro-FastAPI/releases/download/v0.1.4/"
model_version="v0.1.4"

dataset_sha=""
model_sha=""

sha256_file() {
  local path="$1"
  "${WRITER_PY_CMD[@]}" - "$path" <<'PY'
import hashlib, sys
p = sys.argv[1]
h = hashlib.sha256()
with open(p, "rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
}

verify_model_dir() {
  "${PY_CMD[@]}" - <<'PY' "$MODEL_OUT_DIR"
import json, os, sys
root = sys.argv[1]
mf = os.path.join(root, "kokoro-v1_0.pth")
cf = os.path.join(root, "config.json")
if not os.path.exists(mf) or os.path.getsize(mf) <= 0:
    sys.exit(2)
if not os.path.exists(cf) or os.path.getsize(cf) <= 0:
    sys.exit(3)
try:
    json.load(open(cf, "r", encoding="utf-8"))
except Exception:
    sys.exit(4)
sys.exit(0)
PY
}

if [[ "$stage_status" == "success" ]]; then
  # Dataset preparation (minimal prompts)
  if [[ -f "$DATASET_FILE_CACHE" && -f "$RESULTS_JSON" ]]; then
    prev_sha="$("${PY_CMD[@]}" - <<'PY' "$RESULTS_JSON"
import json, sys
try:
    data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
    print((data.get("assets", {}) or {}).get("dataset", {}).get("sha256", "") or "")
except Exception:
    print("")
PY
)"
    cur_sha="$(sha256_file "$DATASET_FILE_CACHE" || true)"
    if [[ -n "$prev_sha" && -n "$cur_sha" && "$prev_sha" == "$cur_sha" ]]; then
      echo "Dataset cache sha256 matches previous run; reusing."
    else
      printf "Hello world!\n" >"$DATASET_FILE_CACHE"
    fi
  else
    printf "Hello world!\n" >"$DATASET_FILE_CACHE"
  fi
  cp -f "$DATASET_FILE_CACHE" "$DATASET_FILE"
  dataset_sha="$(sha256_file "$DATASET_FILE" || true)"

  # Model download to cache (skip if sha matches previous run)
  need_download=1
  if [[ -f "$MODEL_FILE_CACHE" && -f "$MODEL_CONFIG_CACHE" && -f "$RESULTS_JSON" ]]; then
    prev_model_sha="$("${PY_CMD[@]}" - <<'PY' "$RESULTS_JSON"
import json, sys
try:
    data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
    print((data.get("assets", {}) or {}).get("model", {}).get("sha256", "") or "")
except Exception:
    print("")
PY
)"
    cur_model_sha="$(sha256_file "$MODEL_FILE_CACHE" || true)"
    if [[ -n "$prev_model_sha" && -n "$cur_model_sha" && "$prev_model_sha" == "$cur_model_sha" ]]; then
      echo "Model cache sha256 matches previous run; skipping download."
      need_download=0
    fi
  fi

  if [[ "$need_download" -eq 1 ]]; then
    echo "Downloading model into cache: $MODEL_CACHE_DIR"
    set +e
    "${PY_CMD[@]}" docker/scripts/download_model.py --output "$MODEL_CACHE_DIR"
    dl_rc=$?
    set -e

    if [[ "$dl_rc" -ne 0 ]]; then
      echo "Model download script failed with exit code: $dl_rc" >&2
      # Offline reuse: proceed if cache already valid.
      if [[ -f "$MODEL_FILE_CACHE" && -f "$MODEL_CONFIG_CACHE" ]]; then
        echo "Proceeding with existing cached model files despite download failure."
      else
        stage_status="failure"
        failure_category="download_failed"
        exit_code=1
      fi
    fi
  fi

  if [[ "$stage_status" == "success" ]]; then
    # Copy from cache into benchmark_assets/model
    if [[ ! -f "$MODEL_FILE_CACHE" || ! -f "$MODEL_CONFIG_CACHE" ]]; then
      echo "Downloader did not produce expected model artifacts under: $MODEL_CACHE_DIR" >&2
      stage_status="failure"
      failure_category="model"
      exit_code=1
    else
      cp -f "$MODEL_FILE_CACHE" "$MODEL_FILE"
      cp -f "$MODEL_CONFIG_CACHE" "$MODEL_CONFIG"

      if ! verify_model_dir; then
        echo "Model directory could not be verified: $MODEL_OUT_DIR" >&2
        stage_status="failure"
        failure_category="model"
        exit_code=1
      else
        model_sha="$(sha256_file "$MODEL_FILE" || true)"
      fi
    fi
  fi
fi

git_commit=""
if command -v git >/dev/null 2>&1 && [[ -d "$REPO_ROOT/.git" ]]; then
  git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
fi

# Always write results.json.
"${WRITER_PY_CMD[@]}" - <<PY
import json, os, sys, time

payload = {
  "status": "$stage_status",
  "skip_reason": "$skip_reason",
  "exit_code": int("$exit_code"),
  "stage": "prepare",
  "task": "download",
  "command": "$python_cmd_str docker/scripts/download_model.py --output $MODEL_CACHE_DIR (plus dataset prompt generation)",
  "timeout_sec": 1200,
  "framework": "unknown",
  "assets": {
    "dataset": {
      "path": "$DATASET_FILE",
      "source": "$dataset_source",
      "version": "$dataset_version",
      "sha256": "$dataset_sha",
    },
    "model": {
      "path": "$MODEL_OUT_DIR",
      "source": "$model_source",
      "version": "$model_version",
      "sha256": "$model_sha",
    },
  },
  "meta": {
    "python": "$python_cmd_str",
    "git_commit": "$git_commit",
    "env_vars": {
      "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON",""),
      "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT",""),
    },
    "decision_reason": "$decision_reason",
  },
  "failure_category": "$failure_category" if "$stage_status" == "failure" else "",
  "error_excerpt": "",
}

# Re-read log tail here (avoid shell interpolation/escaping issues).
try:
  with open("$LOG_FILE","r",encoding="utf-8",errors="replace") as f:
    lines = f.read().splitlines()[-200:]
    payload["error_excerpt"] = "\n".join(lines)
except Exception:
  payload["error_excerpt"] = ""

with open("$RESULTS_JSON","w",encoding="utf-8") as f:
  json.dump(payload,f,ensure_ascii=False,indent=2)
  f.write("\n")
PY

exit "$exit_code"
