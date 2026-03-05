# SpecForge Reproducible Benchmark Workflow (Scripts Only)

Run the full end-to-end workflow from the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

This runs (in order): `pyright` → `prepare` → `cpu` → `cuda` → `single_gpu` → `multi_gpu` → `env_size` → `hallucination` → `summary`.

## Outputs

All outputs are written under:

- `build_output/<stage>/log.txt`
- `build_output/<stage>/results.json`

Assets are stored under:

- `benchmark_assets/cache/`
- `benchmark_assets/dataset/`
- `benchmark_assets/model/`

## Asset Selection (Default)

`benchmark_scripts/prepare_assets.sh` defaults to:

- Dataset: ShareGPT (`sharegpt`) prepared via `scripts/prepare_data.py` with a small sample
- Model: a small public Qwen checkpoint (tries `Qwen/Qwen2.5-0.5B-Instruct` first)

Override via environment variables:

```bash
export SPECFORGE_BENCH_DATASET=sharegpt
export SPECFORGE_BENCH_DATASET_SAMPLE_SIZE=8
export SPECFORGE_BENCH_MODEL_ID=Qwen/Qwen2.5-0.5B-Instruct
export SPECFORGE_BENCH_MODEL_REVISION=   # optional (tag or commit hash)
```

## Python Interpreter Resolution

Most stages use the Python interpreter from the agent report by default:

- Report path: `$SCIMLOPSBENCH_REPORT` or `/opt/scimlopsbench/report.json`
- Python path: `python_path` inside that report

You can override via:

```bash
export SCIMLOPSBENCH_PYTHON=/path/to/python
```

Or per-stage:

```bash
bash benchmark_scripts/run_single_gpu_entrypoint.sh --python /path/to/python
```

## Notes

- `cpu` stage is marked `skipped` because `scripts/train_eagle3.py` unconditionally uses CUDA (e.g., `.cuda()` / `device="cuda"`), so CPU training is not supported by the native entrypoint.
- `multi_gpu` stage requires ≥2 GPUs; if fewer are available, it exits with failure (as required by the benchmark spec).

