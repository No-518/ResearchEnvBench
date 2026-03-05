#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal single-GPU inference using the repository entrypoint (api.py).

Forces:
  CUDA_VISIBLE_DEVICES=0

Outputs:
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

Optional:
  --timeout-sec <int>            Default: 600
  --python <path>                Override python (otherwise resolved from report.json)
  --report-path <path>           Default: /opt/scimlopsbench/report.json
EOF
}

timeout_sec=600
python_bin=""
report_path=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="build_output/single_gpu"
ASSETS_ROOT="benchmark_assets"
CACHE_ROOT="$ASSETS_ROOT/cache"
HOME_DIR="$CACHE_ROOT/home"
XDG_CACHE_HOME="$CACHE_ROOT/xdg_cache"
HF_HOME="$CACHE_ROOT/hf_home"
HF_HUB_CACHE="$CACHE_ROOT/hf_hub"
HF_DATASETS_CACHE="$CACHE_ROOT/hf_datasets"
TRANSFORMERS_CACHE="$CACHE_ROOT/transformers"
TORCH_HOME="$CACHE_ROOT/torch"

mkdir -p "$OUT_DIR" "$CACHE_ROOT" "$HOME_DIR" "$XDG_CACHE_HOME" "$HF_HOME" "$HF_HUB_CACHE" "$HF_DATASETS_CACHE" "$TRANSFORMERS_CACHE" "$TORCH_HOME"

audio_path="$ASSETS_ROOT/dataset/asr_example_en.wav"
if [[ -f "build_output/prepare/results.json" ]]; then
  audio_path="$(python - <<'PY'
import json, pathlib
p = pathlib.Path("build_output/prepare/results.json")
try:
    d = json.loads(p.read_text(encoding="utf-8"))
    print(d.get("assets", {}).get("dataset", {}).get("path", "benchmark_assets/dataset/asr_example_en.wav"))
except Exception:
    print("benchmark_assets/dataset/asr_example_en.wav")
PY
)"
fi

runner_py="$(command -v python3 || command -v python)"

cmd="$(cat <<'BASH'
set -euo pipefail

"$BENCH_PYTHON" - <<'PY'
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

repo_root = pathlib.Path(os.environ["BENCH_REPO_ROOT"]).resolve()
out_dir = pathlib.Path(os.environ["BENCH_STAGE_OUT_DIR"]).resolve()
audio_path = pathlib.Path(os.environ["BENCH_AUDIO"]).resolve()
port = int(os.environ.get("BENCH_PORT", "50000"))

if not (repo_root / "api.py").is_file():
    raise SystemExit("entrypoint_not_found: api.py is missing")
if not audio_path.is_file():
    raise SystemExit(f"data: missing audio file: {audio_path}")

# Quick pre-check to produce clearer errors than a FastAPI stack trace.
try:
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
        raise RuntimeError(f"CUDA not available (is_available={torch.cuda.is_available()}, device_count={torch.cuda.device_count()})")
except Exception as e:
    raise SystemExit(f"runtime: single-gpu precheck failed: {e}")

server_env = dict(os.environ)
server_env["CUDA_VISIBLE_DEVICES"] = "0"
server_env["SENSEVOICE_DEVICE"] = "cuda:0"
server_env["PYTHONDONTWRITEBYTECODE"] = "1"
server_env["PYTHONUNBUFFERED"] = "1"

server = None
try:
    server = subprocess.Popen(
        [sys.executable, str(repo_root / "api.py")],
        cwd=str(repo_root),
        env=server_env,
        stdout=None,
        stderr=None,
        text=False,
    )

    ready_url = f"http://127.0.0.1:{port}/"
    for _ in range(90):
        if server.poll() is not None:
            raise RuntimeError(f"api.py exited early with code {server.returncode}")
        try:
            with urllib.request.urlopen(ready_url, timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError("api.py did not become ready within 90s")

    boundary = f"----scimlopsbench-{uuid.uuid4().hex}"
    file_bytes = audio_path.read_bytes()
    filename = audio_path.name

    def part(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(part("lang", "auto"))
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(
        url=f"http://127.0.0.1:{port}/api/v1/asr",
        method="POST",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp_bytes = r.read()
        resp_text = resp_bytes.decode("utf-8", errors="replace")

    (out_dir / "response.json").write_text(resp_text + "\n", encoding="utf-8")
    try:
        parsed = json.loads(resp_text)
    except Exception as e:
        raise RuntimeError(f"invalid json response: {e}")
    if not isinstance(parsed, dict) or "result" not in parsed:
        raise RuntimeError("unexpected response schema (missing 'result')")

finally:
    if server and server.poll() is None:
        try:
            server.send_signal(signal.SIGTERM)
            server.wait(timeout=10)
        except Exception:
            try:
                server.kill()
            except Exception:
                pass
PY
BASH
)"

exec "$runner_py" benchmark_scripts/runner.py \
  --stage single_gpu \
  --task infer \
  --framework pytorch \
  --timeout-sec "$timeout_sec" \
  --out-dir "$OUT_DIR" \
  --requires-python \
  ${report_path:+--report-path "$report_path"} \
  ${python_bin:+--python "$python_bin"} \
  --decision-reason "Uses repo entrypoint api.py with SENSEVOICE_DEVICE=cuda:0 and a single POST request (one audio) as the minimal single-GPU inference step." \
  --env "BENCH_REPO_ROOT=$REPO_ROOT" \
  --env "BENCH_STAGE_OUT_DIR=$OUT_DIR" \
  --env "BENCH_AUDIO=$audio_path" \
  --env "BENCH_PORT=50000" \
  --env "CUDA_VISIBLE_DEVICES=0" \
  --env "SENSEVOICE_DEVICE=cuda:0" \
  --env "HOME=$HOME_DIR" \
  --env "XDG_CACHE_HOME=$XDG_CACHE_HOME" \
  --env "HF_HOME=$HF_HOME" \
  --env "HF_HUB_CACHE=$HF_HUB_CACHE" \
  --env "HF_DATASETS_CACHE=$HF_DATASETS_CACHE" \
  --env "TRANSFORMERS_CACHE=$TRANSFORMERS_CACHE" \
  --env "TORCH_HOME=$TORCH_HOME" \
  --env "PYTHONDONTWRITEBYTECODE=1" \
  --env "PYTHONUNBUFFERED=1" \
  -- bash -lc "$cmd"

