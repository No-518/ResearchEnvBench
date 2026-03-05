# Benchmark Scripts (ScimlOpsBench)

These scripts add a fully connected, reproducible benchmark workflow to this repository **without modifying any existing repo files**.

## One-command run

```bash
bash benchmark_scripts/run_all.sh
```

This runs (in order): `pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary` and **does not abort early** on failures.

Outputs are written to:
- `benchmark_assets/` (cached dataset/model + manifest)
- `build_output/<stage>/` (per-stage `log.txt` + `results.json`)
- `build_output/summary/results.json` (final rollup)

## Agent report (required)

Default agent report path:
- `/opt/scimlopsbench/report.json`

Override priority:
1) CLI `--report-path`
2) `SCIMLOPSBENCH_REPORT`
3) default path above

## Common options

Pass these to `run_all.sh` (forwarded to stages where applicable):

```bash
bash benchmark_scripts/run_all.sh \
  --report-path /opt/scimlopsbench/report.json \
  --python /opt/scimlopsbench/python \
  --out-dir build_output
```

## Asset selection

Defaults are chosen from the repository’s official examples:
- dataset: `finetrainers/crush-smol`
- model: `Wan-AI/Wan2.1-T2V-1.3B-Diffusers` (model_name=`wan`, training_type=`lora`)

For LoRA runs, the entrypoint scripts auto-select `--target_modules` based on `model_name` from the official example `train.sh` files. Override manually if needed:

```bash
export BENCH_TARGET_MODULES='blocks.*(to_q|to_k|to_v|to_out.0)'
```

Override via env vars for `prepare_assets.sh`:

```bash
export BENCH_DATASET_ID="finetrainers/crush-smol"
export BENCH_DATASET_REVISION=""          # optional
export BENCH_MODEL_NAME="wan"             # must be one of: wan, ltx_video, cogvideox, ...
export BENCH_MODEL_ID="Wan-AI/Wan2.1-T2V-1.3B-Diffusers"
export BENCH_MODEL_REVISION=""            # optional
```

If downloads are gated, set one of:
- `HF_TOKEN`
- `HUGGINGFACE_HUB_TOKEN`

## Notes on failure detection

The repository `train.py` logs exceptions but may still exit with code `0`. The benchmark runner uses log-based failure detection (regex matching for the repo’s `An error occurred during training:` + tracebacks) so `build_output/*/results.json` reflects the real outcome.

The default dataset is a HuggingFace video dataset; runtime may require `torchcodec` and FFmpeg shared libraries. The benchmark scripts do **not** install system dependencies; if these are missing, stages will fail with `failure_category=deps` and the root error will appear in the stage log.

## Running individual stages

- Pyright missing-import check:
  - `bash benchmark_scripts/run_pyright_missing_imports.sh --repo .`
- Asset prep:
  - `bash benchmark_scripts/prepare_assets.sh`
- CPU 1-step entrypoint run:
  - `bash benchmark_scripts/run_cpu_entrypoint.sh`
- CUDA availability check:
  - `python3 benchmark_scripts/check_cuda_available.py`
- Single-GPU 1-step entrypoint run:
  - `bash benchmark_scripts/run_single_gpu_entrypoint.sh`
- Multi-GPU 1-step entrypoint run:
  - `bash benchmark_scripts/run_multi_gpu_entrypoint.sh --gpu-ids 0,1`
- Environment size:
  - `python3 benchmark_scripts/measure_env_size.py`
- Agent report + hallucination validation:
  - `python3 benchmark_scripts/validate_agent_report.py`
- Summary:
  - `python3 benchmark_scripts/summarize_results.py`
