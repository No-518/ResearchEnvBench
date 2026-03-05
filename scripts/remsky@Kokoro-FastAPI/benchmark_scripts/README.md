# Benchmark Scripts (Reproducible Workflow)

Run the full benchmark chain from the repo root:

```bash
bash benchmark_scripts/run_all.sh
```

## Inputs

- Agent report (default): `/opt/scimlopsbench/report.json`
  - Override with: `SCIMLOPSBENCH_REPORT=/path/to/report.json`
- Python interpreter:
  - By default `run_all.sh` uses `python_path` from the report and exports it as `SCIMLOPSBENCH_PYTHON`.
  - You can override stages by passing `--python <cmd>` to the individual stage scripts.

## What It Does (in order)

1. `run_pyright_missing_imports.sh` — Pyright missing-import diagnostics only.
2. `prepare_assets.sh` — Creates `benchmark_assets/` and downloads the minimal model weights (plus a tiny text prompt dataset).
3. `run_cpu_entrypoint.sh` — Starts the repo’s FastAPI entrypoint on CPU and performs exactly one `/v1/audio/speech` inference request.
4. `check_cuda_available.py` — Detects CUDA availability and GPU count.
5. `run_single_gpu_entrypoint.sh` — Starts the entrypoint on a single GPU and performs one inference request.
6. `run_multi_gpu_entrypoint.sh` — Skips if <2 GPUs or if repo has no multi-GPU distributed entrypoint.
7. `measure_env_size.py` — Measures environment footprint via the report’s `python_path`.
8. `validate_agent_report.py` — Validates the agent report and outputs hallucination statistics.
9. `summarize_results.py` — Aggregates per-stage results into `build_output/summary/results.json`.

## Outputs

- Per-stage outputs: `build_output/<stage>/{log.txt,results.json}`
- Assets:
  - `benchmark_assets/cache/` (download cache)
  - `benchmark_assets/dataset/` (dataset)
  - `benchmark_assets/model/` (model weights)

## Notes

- Network is required to download model weights unless the cache already exists.
- The CPU/GPU stages set `PYTHONDONTWRITEBYTECODE=1` and redirect temp directories into `build_output/<stage>/` to avoid writing outside the benchmark output directories.

