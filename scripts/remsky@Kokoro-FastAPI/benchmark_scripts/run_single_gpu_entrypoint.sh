#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal single-GPU inference via the repository's native entrypoint (uvicorn FastAPI app).

Outputs (always written):
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

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

OUT_DIR="$REPO_ROOT/build_output/single_gpu"
mkdir -p "$OUT_DIR"
LOG_FILE="$OUT_DIR/log.txt"
RESULTS_JSON="$OUT_DIR/results.json"

exec > >(tee "$LOG_FILE") 2>&1

cd "$repo"

stage_status="success"
failure_category=""
skip_reason="unknown"
exit_code=0
timeout_sec=600
framework="pytorch"

# Avoid writing __pycache__ into the repository.
export PYTHONDONTWRITEBYTECODE=1

PY_CMD=(python)
PY_SOURCE=""

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
    PY_SOURCE="missing_report"
    return 1
  fi

  local sys_py
  if command -v python3 >/dev/null 2>&1; then
    sys_py="python3"
  elif command -v python >/dev/null 2>&1; then
    sys_py="python"
  else
    PY_SOURCE="missing_report"
    return 1
  fi

  local resolved
  resolved="$("$sys_py" - <<'PY'
import json, os, sys
path = os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"
data = json.load(open(path, "r", encoding="utf-8"))
pp = data.get("python_path")
if not isinstance(pp, str) or not pp.strip():
    raise SystemExit(2)
print(pp)
PY
)" || return 1

  # shellcheck disable=SC2206
  PY_CMD=($resolved)
  PY_SOURCE="report"
  return 0
}

if ! resolve_python; then
  echo "Failed to resolve python (missing report and no --python/SCIMLOPSBENCH_PYTHON)." >&2
  stage_status="failure"
  failure_category="missing_report"
  exit_code=1
fi

python_cmd_str="${PY_CMD[*]}"
echo "Repo: $repo"
echo "Python cmd: $python_cmd_str (source=$PY_SOURCE)"

WRITER_PY_CMD=("${PY_CMD[@]}")
if ! "${WRITER_PY_CMD[@]}" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    WRITER_PY_CMD=(python3)
  elif command -v python >/dev/null 2>&1; then
    WRITER_PY_CMD=(python)
  fi
fi

gpu_count=0
cuda_available=0
if [[ "$stage_status" == "success" ]]; then
  set +e
  read -r gpu_count cuda_available < <("${WRITER_PY_CMD[@]}" - <<'PY'
try:
    import torch
    print(torch.cuda.device_count(), 1 if torch.cuda.is_available() else 0)
except Exception:
    print(0, 0)
PY
)
  set -e
fi

echo "Observed GPU count: ${gpu_count:-0} (cuda_available=${cuda_available:-0})"

if [[ "$stage_status" == "success" && "${cuda_available:-0}" -eq 0 ]]; then
  echo "CUDA is not available; single-GPU stage requires CUDA." >&2
  stage_status="failure"
  failure_category="hardware"
  exit_code=1
fi

SERVER_PID=""
cleanup() {
  if [[ -n "${SERVER_PID:-}" ]]; then
    if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
      echo "Stopping server (pid=$SERVER_PID)"
      kill -INT "$SERVER_PID" >/dev/null 2>&1 || true
      for _ in {1..30}; do
        if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
          break
        fi
        sleep 0.2
      done
      kill -KILL "$SERVER_PID" >/dev/null 2>&1 || true
    fi
  fi
}
trap cleanup EXIT

DATASET_PATH="$REPO_ROOT/benchmark_assets/dataset/prompts.txt"
MODEL_ROOT="$REPO_ROOT/benchmark_assets/model"
MODEL_FILE="$MODEL_ROOT/v1_0/kokoro-v1_0.pth"
MODEL_CONFIG="$MODEL_ROOT/v1_0/config.json"
VOICES_DIR_REL="src/voices/v1_0"

if [[ "$stage_status" == "success" ]]; then
  if [[ ! -f "$DATASET_PATH" ]]; then
    echo "Dataset not found: $DATASET_PATH" >&2
    stage_status="failure"
    failure_category="data"
    exit_code=1
  fi
fi

if [[ "$stage_status" == "success" ]]; then
  if [[ ! -f "$MODEL_FILE" || ! -f "$MODEL_CONFIG" ]]; then
    echo "Model not found under: $MODEL_ROOT (expected $MODEL_FILE and $MODEL_CONFIG)" >&2
    stage_status="failure"
    failure_category="model"
    exit_code=1
  fi
fi

PORT=""
INPUT_TEXT=""
SAMPLE_AUDIO_PATH="$OUT_DIR/sample_gpu.mp3"

if [[ "$stage_status" == "success" ]]; then
  INPUT_TEXT="$(head -n 1 "$DATASET_PATH" | tr -d '\r' || true)"
  if [[ -z "$INPUT_TEXT" ]]; then
    INPUT_TEXT="Hello world!"
  fi

  PORT="$("${WRITER_PY_CMD[@]}" - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

  export CUDA_VISIBLE_DEVICES=0
  export USE_GPU=true
  export DEVICE_TYPE=cuda
  export MODEL_DIR="$MODEL_ROOT"
  export VOICES_DIR="$VOICES_DIR_REL"
  export TEMP_FILE_DIR="$OUT_DIR/temp_files"
  export TMPDIR="$OUT_DIR/tmp"
  export PYTHONPATH="$REPO_ROOT:$REPO_ROOT/api"

  mkdir -p "$TEMP_FILE_DIR" "$TMPDIR"

  uvicorn_cmd=("${PY_CMD[@]}" -m uvicorn api.src.main:app --host 127.0.0.1 --port "$PORT" --log-level info)
  echo "Starting server: ${uvicorn_cmd[*]}"
  "${uvicorn_cmd[@]}" &
  SERVER_PID=$!

  echo "Waiting for /health on port $PORT..."
  if ! "${WRITER_PY_CMD[@]}" - <<-PY; then
import time, urllib.request, sys
port = int("$PORT")
url = f"http://127.0.0.1:{port}/health"
deadline = time.time() + 180
last_err = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            if r.status == 200:
                sys.exit(0)
    except Exception as e:
        last_err = e
    time.sleep(0.5)
	print(f"Server did not become healthy in time. Last error: {last_err}", file=sys.stderr)
	sys.exit(1)
	PY
	    echo "Server failed to become healthy" >&2
	    stage_status="failure"
	    failure_category="runtime"
	    exit_code=1
	  fi
fi

if [[ "$stage_status" == "success" ]]; then
  echo "Sending one inference request (single GPU)..."
  if ! "${WRITER_PY_CMD[@]}" - <<-PY; then
import json, sys, urllib.request
port = int("$PORT")
url = f"http://127.0.0.1:{port}/v1/audio/speech"
payload = {
  "model": "kokoro",
  "input": "$INPUT_TEXT",
  "voice": "af_heart",
  "response_format": "mp3",
  "stream": False,
}
data = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, timeout=300) as r:
    out = r.read()
if not out or len(out) < 256:
    print(f"Unexpected audio response size: {len(out) if out else 0}", file=sys.stderr)
    sys.exit(2)
	open("$SAMPLE_AUDIO_PATH", "wb").write(out)
	print(f"Wrote {len(out)} bytes to $SAMPLE_AUDIO_PATH")
	sys.exit(0)
	PY
	    echo "Inference request failed" >&2
	    stage_status="failure"
	    failure_category="runtime"
	    exit_code=1
	  fi
fi

git_commit=""
if command -v git >/dev/null 2>&1 && [[ -d "$REPO_ROOT/.git" ]]; then
  git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
fi

prepare_results="$REPO_ROOT/build_output/prepare/results.json"

"${WRITER_PY_CMD[@]}" - <<PY
import json, os
from pathlib import Path

def safe_load(path: Path):
  try:
    return json.loads(path.read_text(encoding="utf-8"))
  except Exception:
    return None

assets = {
  "dataset": {"path": "$DATASET_PATH", "source": "", "version": "", "sha256": ""},
  "model": {"path": "$MODEL_ROOT/v1_0", "source": "", "version": "", "sha256": ""},
}

prep = safe_load(Path("$prepare_results"))
if isinstance(prep, dict):
  a = (prep.get("assets") or {})
  if isinstance(a.get("dataset"), dict):
    assets["dataset"].update({k: a["dataset"].get(k,"") for k in ["path","source","version","sha256"]})
  if isinstance(a.get("model"), dict):
    assets["model"].update({k: a["model"].get(k,"") for k in ["path","source","version","sha256"]})

try:
  lines = Path("$LOG_FILE").read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
  excerpt = "\n".join(lines)
except Exception:
  excerpt = ""

payload = {
  "status": "$stage_status",
  "skip_reason": "$skip_reason",
  "exit_code": int("$exit_code"),
  "stage": "single_gpu",
  "task": "infer",
  "command": "$python_cmd_str -m uvicorn api.src.main:app --host 127.0.0.1 --port $PORT ; POST /v1/audio/speech (1 request)",
  "timeout_sec": $timeout_sec,
  "framework": "$framework",
  "assets": assets,
  "meta": {
    "python": "$python_cmd_str",
    "git_commit": "$git_commit",
    "env_vars": {
      "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES",""),
      "USE_GPU": os.environ.get("USE_GPU",""),
      "DEVICE_TYPE": os.environ.get("DEVICE_TYPE",""),
      "MODEL_DIR": os.environ.get("MODEL_DIR",""),
      "VOICES_DIR": os.environ.get("VOICES_DIR",""),
      "TEMP_FILE_DIR": os.environ.get("TEMP_FILE_DIR",""),
      "TMPDIR": os.environ.get("TMPDIR",""),
      "PYTHONPATH": os.environ.get("PYTHONPATH",""),
    },
    "decision_reason": "Single-GPU run via uvicorn FastAPI entrypoint; one HTTP inference request with batch_size=1 equivalent; CUDA_VISIBLE_DEVICES=0.",
    "port": "$PORT",
    "sample_audio_path": "$SAMPLE_AUDIO_PATH" if Path("$SAMPLE_AUDIO_PATH").exists() else "",
  },
  "failure_category": "$failure_category" if "$stage_status" == "failure" else "",
  "error_excerpt": excerpt,
}

Path("$RESULTS_JSON").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

exit "$exit_code"
