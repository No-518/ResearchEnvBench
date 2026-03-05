#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a minimal CPU inference via repository entrypoint.

Defaults:
  - Uses python_path from agent report.json (SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)
  - Uses assets from build_output/prepare/results.json

Optional:
  --python <path>          Explicit python executable to use (highest priority)
  --report-path <path>     Agent report.json path override
  --timeout-sec <int>      Default: 600
EOF
}

python_bin=""
report_path=""
timeout_sec="600"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_bin="${2:-}"; shift 2 ;;
    --report-path) report_path="${2:-}"; shift 2 ;;
    --timeout-sec) timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

BOOTSTRAP_PY="$(command -v python >/dev/null 2>&1 && echo python || echo python3)"

prepare_results="build_output/prepare/results.json"
if [[ ! -s "$prepare_results" ]]; then
  mkdir -p build_output/cpu
  printf "Missing %s; run prepare_assets.sh first.\n" "$prepare_results" > build_output/cpu/log.txt
  "$BOOTSTRAP_PY" - <<'PY'
import json
from pathlib import Path

out = Path("build_output/cpu/results.json")
payload = {
  "status": "failure",
  "skip_reason": "not_applicable",
  "exit_code": 1,
  "stage": "cpu",
  "task": "infer",
  "command": "",
  "timeout_sec": 600,
  "framework": "pytorch",
  "assets": {
    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
    "model": {"path": "", "source": "", "version": "", "sha256": ""},
  },
  "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "prepare stage missing"},
  "failure_category": "data",
  "error_excerpt": f"Missing {Path('build_output/prepare/results.json')}",
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
fi

fail_with_results() {
  local failure_category="$1"
  local message="$2"
  mkdir -p build_output/cpu
  printf "%s\n" "$message" > build_output/cpu/log.txt
  BENCH_CPU_REPO_ROOT="$repo_root" \
  BENCH_CPU_PREPARE_RESULTS="$prepare_results" \
  BENCH_CPU_FAILURE_CATEGORY="$failure_category" \
  BENCH_CPU_MESSAGE="$message" \
  BENCH_CPU_TIMEOUT_SEC="$timeout_sec" \
  "$BOOTSTRAP_PY" - <<'PY'
import json
import os
from pathlib import Path

repo_root = Path(os.environ["BENCH_CPU_REPO_ROOT"]).resolve()
out = repo_root / "build_output/cpu/results.json"
prepare_path = repo_root / os.environ["BENCH_CPU_PREPARE_RESULTS"]
assets = {}
try:
    assets = json.load(open(prepare_path, "r", encoding="utf-8")).get("assets", {})
except Exception:
    assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

payload = {
    "status": "failure",
    "skip_reason": "not_applicable",
    "exit_code": 1,
    "stage": "cpu",
    "task": "infer",
    "command": "",
    "timeout_sec": int(os.environ.get("BENCH_CPU_TIMEOUT_SEC", "600")),
    "framework": "pytorch",
    "assets": assets,
    "meta": {"python": "", "git_commit": "", "env_vars": {}, "decision_reason": "asset path resolution failed"},
    "failure_category": os.environ.get("BENCH_CPU_FAILURE_CATEGORY", "unknown"),
    "error_excerpt": os.environ.get("BENCH_CPU_MESSAGE", ""),
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  exit 1
}

infer_dir="$("$BOOTSTRAP_PY" -c 'import json,sys; d=json.load(open(sys.argv[1],"r",encoding="utf-8")); print(d.get("meta",{}).get("dataset",{}).get("infer_dir",""))' "$prepare_results" 2>/dev/null || true)"
ckpt_path="$("$BOOTSTRAP_PY" -c 'import json,sys; d=json.load(open(sys.argv[1],"r",encoding="utf-8")); print(d.get("meta",{}).get("tts_checkpoint_path",""))' "$prepare_results" 2>/dev/null || true)"

ref_audio="$infer_dir/ref_audio.wav"
ref_text_file="$infer_dir/ref_text.txt"
gen_text_file="$infer_dir/gen_text.txt"

ref_text="$(cat "$ref_text_file" 2>/dev/null || true)"
gen_text="$(cat "$gen_text_file" 2>/dev/null || true)"

if [[ -z "$infer_dir" || ! -f "$ref_audio" ]]; then
  fail_with_results "data" "Prepared dataset infer_dir is missing or invalid: infer_dir=$infer_dir ref_audio=$ref_audio"
fi
if [[ -z "$ckpt_path" || ! -f "$ckpt_path" ]]; then
  fail_with_results "model" "Prepared TTS checkpoint path is missing or invalid: $ckpt_path"
fi

runner_args=(run --stage cpu --task infer --framework pytorch --timeout-sec "$timeout_sec" --decision-reason "Use f5_tts.infer.infer_cli with nfe_step=1 forced to CPU; assets from prepare_assets.sh.")
[[ -n "$python_bin" ]] && runner_args+=(--python "$python_bin")
[[ -n "$report_path" ]] && runner_args+=(--report-path "$report_path")

set +e
"$BOOTSTRAP_PY" benchmark_scripts/runner.py "${runner_args[@]}" \
  --env "CUDA_VISIBLE_DEVICES=" \
  --env "HF_HUB_OFFLINE=1" \
  --env "TRANSFORMERS_OFFLINE=1" \
  --py-module f5_tts.infer.infer_cli --py-args \
    --model F5TTS_v1_Base \
    --ckpt_file "$ckpt_path" \
    --ref_audio "$ref_audio" \
    --ref_text "$ref_text" \
    --gen_text "$gen_text" \
    --output_dir build_output/cpu/out \
    --output_file infer_cpu.wav \
    --nfe_step 1 \
    --device cpu
rc=$?
set -e
exit "$rc"
