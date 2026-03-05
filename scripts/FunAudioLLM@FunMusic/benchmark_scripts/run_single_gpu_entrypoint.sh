#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage="single_gpu"
out_dir="$repo_root/build_output/$stage"
mkdir -p "$out_dir"

prepare_results="$repo_root/build_output/prepare/results.json"

if [[ ! -f "$prepare_results" ]]; then
  err="Missing prepare results at $prepare_results"
  echo "$err" >"$out_dir/log.txt"
  python - <<'PY' "$out_dir/results.json" "$err"
import json, sys
out=sys.argv[1]
err=sys.argv[2]
payload={
  "status":"failure",
  "skip_reason":"not_applicable",
  "exit_code":1,
  "stage":"single_gpu",
  "task":"infer",
  "command":"bash benchmark_scripts/run_single_gpu_entrypoint.sh",
  "timeout_sec":600,
  "framework":"pytorch",
  "assets":{"dataset":{"path":"","source":"","version":"","sha256":""},"model":{"path":"","source":"","version":"","sha256":""}},
  "meta":{"python":"","git_commit":"","env_vars":{},"decision_reason":"requires build_output/prepare/results.json"},
  "failure_category":"missing_stage_results",
  "error_excerpt":err,
}
with open(out,"w",encoding="utf-8") as f:
  json.dump(payload,f,ensure_ascii=False,indent=2)
PY
  exit 1
fi

gpu_id="${GPU_ID:-0}"

read_vars="$(
  python - <<'PY' "$prepare_results"
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
d=json.loads(p.read_text(encoding="utf-8"))
assets=d.get("assets",{})
meta=d.get("meta",{})
dataset_list=meta.get("dataset_list_1row","")
bench_cfg=meta.get("benchmark_config_path","")
model_path=(assets.get("model",{}) or {}).get("path","")
print(dataset_list)
print(bench_cfg)
print(model_path)
PY
)"

dataset_list="$(echo "$read_vars" | sed -n '1p')"
bench_cfg="$(echo "$read_vars" | sed -n '2p')"
model_path="$(echo "$read_vars" | sed -n '3p')"

python "$repo_root/benchmark_scripts/runner.py" \
  --stage "$stage" \
  --task infer \
  --framework pytorch \
  --timeout-sec 600 \
  --assets-from "$prepare_results" \
  --decision-reason "Repo entrypoint inspiremusic/bin/inference.py; single-GPU forced via CUDA_VISIBLE_DEVICES=0 and --gpu 0; one sample (1-row parquet list) + fast mode." \
  --env CUDA_VISIBLE_DEVICES="$gpu_id" \
  --env PYTHONIOENCODING=UTF-8 \
  --env PYTHONPATH="$repo_root:$repo_root/third_party/Matcha-TTS:${PYTHONPATH:-}" \
  --env TOKENIZERS_PARALLELISM=false \
  -- \
  python inspiremusic/bin/inference.py \
    --task text-to-music \
    --gpu 0 \
    --config "$bench_cfg" \
    --prompt_data "$dataset_list" \
    --llm_model "$model_path/llm.pt" \
    --music_tokenizer "$model_path/music_tokenizer" \
    --wavtokenizer "$model_path/wavtokenizer" \
    --result_dir "$out_dir/infer_out" \
    --chorus intro \
    --fast \
    --min_generate_audio_seconds 0.0 \
    --max_generate_audio_seconds 1.0 \
    --output_sample_rate 24000 \
    --format wav
