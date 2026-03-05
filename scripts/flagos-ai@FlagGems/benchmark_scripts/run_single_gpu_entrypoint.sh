#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal single-GPU repository entrypoint (1 step).

Default entrypoint:
  pytest examples/model_bert_test.py::test_accuracy_bert[How are you today?-torch.float16]

Outputs:
  build_output/single_gpu/log.txt
  build_output/single_gpu/results.json

Optional:
  --python <path>        Override python interpreter
  --report-path <path>   Override report.json path
EOF
}

python_override=""
report_path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_override="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage_dir="$repo_root/build_output/single_gpu"
mkdir -p "$stage_dir"

hf_home="$repo_root/benchmark_assets/cache/hf_home"
dataset_path="$repo_root/benchmark_assets/dataset/prompts.jsonl"
model_path="$repo_root/benchmark_assets/model/google-bert__bert-base-uncased__tokenizer"

pybin="python3"
command -v python3 >/dev/null 2>&1 || pybin="python"

# Preflight: ensure pytest is available in the resolved environment python.
preflight_log="$stage_dir/preflight.txt"
preflight_meta="$stage_dir/preflight_meta.json"
rm -f "$preflight_log" "$preflight_meta" 2>/dev/null || true
touch "$preflight_log"

resolved_python=""
if [[ -n "$python_override" ]]; then
  resolved_python="$python_override"
elif [[ -n "${SCIMLOPSBENCH_PYTHON:-}" ]]; then
  resolved_python="$SCIMLOPSBENCH_PYTHON"
else
  # Uses the same resolution rules as runner.py (report.json by default).
  print_python_cmd=("$pybin" "$repo_root/benchmark_scripts/runner.py" --print-python --python-required)
  if [[ -n "$report_path" ]]; then
    print_python_cmd+=(--report-path "$report_path")
  fi
  set +e
  resolved_python="$("${print_python_cmd[@]}" 2>/dev/null)"
  set -e
fi

pytest_install_attempted=0
pytest_install_command=""
pytest_install_returncode=""
pytest_install_failure_category=""
pytest_ok=0

if [[ -n "$resolved_python" && -x "$resolved_python" ]]; then
  if "$resolved_python" -c "import pytest" >/dev/null 2>&1; then
    pytest_ok=1
    echo "[single_gpu preflight] pytest already available in: $resolved_python" >>"$preflight_log"
  else
    pytest_install_attempted=1
    pytest_install_command="$resolved_python -m pip install -q pytest"
    echo "[single_gpu preflight] pytest missing; attempting install: $pytest_install_command" >>"$preflight_log"
    set +e
    PIP_CACHE_DIR="$repo_root/benchmark_assets/cache/pip" \
    XDG_CACHE_HOME="$repo_root/benchmark_assets/cache/xdg" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
      "$resolved_python" -m pip install -q pytest >>"$preflight_log" 2>&1
    pytest_install_returncode=$?
    set -e
    if [[ "$pytest_install_returncode" -eq 0 ]] && "$resolved_python" -c "import pytest" >/dev/null 2>&1; then
      pytest_ok=1
      echo "[single_gpu preflight] pytest install succeeded" >>"$preflight_log"
    else
      pytest_ok=0
      echo "[single_gpu preflight] pytest install failed (rc=$pytest_install_returncode)" >>"$preflight_log"
      pytest_install_failure_category="deps"
      if grep -E "Temporary failure|Name or service not known|Connection|timed out|CERTIFICATE|No matching distribution" "$preflight_log" >/dev/null 2>&1; then
        pytest_install_failure_category="download_failed"
      fi
    fi
  fi
else
  echo "[single_gpu preflight] Could not resolve an executable python for installation checks." >>"$preflight_log"
fi

"$pybin" - <<PY
import json, os, pathlib
meta_path = pathlib.Path(${preflight_meta@Q})
log_path = pathlib.Path(${preflight_log@Q})
payload = {
  "preflight": {
    "resolved_python": os.environ.get("RESOLVED_PYTHON", ${resolved_python@Q}),
    "pytest_ok": bool(int(os.environ.get("PYTEST_OK", "${pytest_ok}"))),
    "pytest_install": {
      "attempted": bool(int(os.environ.get("PYTEST_INSTALL_ATTEMPTED", "${pytest_install_attempted}"))),
      "command": os.environ.get("PYTEST_INSTALL_COMMAND", ${pytest_install_command@Q}),
      "returncode": os.environ.get("PYTEST_INSTALL_RC", ${pytest_install_returncode@Q}),
      "failure_category": os.environ.get("PYTEST_INSTALL_FAILURE_CATEGORY", ${pytest_install_failure_category@Q}),
    },
    "preflight_log_tail": "",
  }
}
try:
  lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
  payload["preflight"]["preflight_log_tail"] = "\n".join(lines[-120:])
except Exception:
  pass
meta_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
PY

runner_args=(
  --stage single_gpu
  --task infer
  --framework pytorch
  --timeout-sec 600
  --out-dir "$stage_dir"
  --assets-from-prepare
  --extra-meta-json-path "$preflight_meta"
  --decision-reason "Use repo-documented model accuracy test (docs/pytest_in_flaggems.md) with a single parametrized case to enforce batch_size=1 and steps=1."
  --env "PYTHONPATH=$repo_root/src"
  --env "CUDA_VISIBLE_DEVICES=0"
  --env "HF_HOME=$hf_home"
  --env "HF_HUB_DISABLE_TELEMETRY=1"
  --env "HF_HUB_OFFLINE=1"
  --env "TRANSFORMERS_OFFLINE=1"
  --env "TOKENIZERS_PARALLELISM=false"
  --env "XDG_CACHE_HOME=$repo_root/benchmark_assets/cache/xdg"
  --env "TRITON_CACHE_DIR=$repo_root/benchmark_assets/cache/triton"
  --env "TORCH_HOME=$repo_root/benchmark_assets/cache/torch"
  --env "TORCH_EXTENSIONS_DIR=$repo_root/benchmark_assets/cache/torch_extensions"
  --env "TORCHINDUCTOR_CACHE_DIR=$repo_root/benchmark_assets/cache/torchinductor"
  --env "FLAGGEMS_CACHE_DIR=$repo_root/benchmark_assets/cache/flaggems"
  --env "SCIMLOPSBENCH_DATASET_PATH=$dataset_path"
  --env "SCIMLOPSBENCH_MODEL_PATH=$model_path"
)

if [[ -n "$report_path" ]]; then
  runner_args+=(--report-path "$report_path")
fi
if [[ -n "$python_override" ]]; then
  runner_args+=(--python "$python_override" --python-required)
else
  runner_args+=(--python-required)
fi
if [[ "$pytest_ok" -ne 1 && -n "$pytest_install_failure_category" ]]; then
  runner_args+=(--failure-category "$pytest_install_failure_category")
fi

# Collect the first available parametrized BERT accuracy test nodeid dynamically,
# then run only that nodeid. This avoids hardcoding pytest's parametrized id format.
impl_py="$stage_dir/_single_gpu_impl.py"
cat >"$impl_py" <<'PY'
import os
import subprocess
import sys


def main() -> int:
    collect_cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "examples/model_bert_test.py",
        "-k",
        "test_accuracy_bert",
    ]
    print("[single_gpu] collect_cmd:", " ".join(collect_cmd), flush=True)
    p = subprocess.run(
        collect_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    print(p.stdout, end="", flush=True)
    if p.returncode != 0:
        print(f"[single_gpu] pytest collection failed: rc={p.returncode}", flush=True)
        return 16

    nodeid = ""
    for line in (p.stdout or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if "::test_accuracy_bert" in s and "model_bert_test.py" in s:
            nodeid = s
            break

    if not nodeid:
        print("[single_gpu] No matching nodeid found for test_accuracy_bert", flush=True)
        return 16

    run_cmd = [sys.executable, "-m", "pytest", "-q", nodeid]
    print("[single_gpu] run_cmd:", " ".join(run_cmd), flush=True)
    r = subprocess.run(run_cmd, check=False, env=os.environ.copy())
    return int(r.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
PY

"$pybin" "$repo_root/benchmark_scripts/runner.py" "${runner_args[@]}" -- \
  "{python}" "$impl_py"
