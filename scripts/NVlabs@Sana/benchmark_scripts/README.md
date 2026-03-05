# Benchmark Scripts (Reproducible Workflow)

Run the full benchmark (always executes all stages, does not stop early on failures):

```bash
bash benchmark_scripts/run_all.sh
```

All outputs go under `build_output/` and all downloaded assets/caches go under `benchmark_assets/`.

## Configuration

- Report path (default: `/opt/scimlopsbench/report.json`):
  - `SCIMLOPSBENCH_REPORT=/path/to/report.json`
- HuggingFace token (only if required by gated assets):
  - `export HF_TOKEN=...`
- Multi-GPU overrides:
  - `SCIMLOPSBENCH_MULTI_GPU_CUDA_VISIBLE_DEVICES=0,1`
  - `SCIMLOPSBENCH_MULTI_GPU_NPROC=2`

## Stages

Execution order (see `benchmark_scripts/run_all.sh`):

1. `benchmark_scripts/run_pyright_missing_imports.sh`
2. `benchmark_scripts/prepare_assets.sh`
3. `benchmark_scripts/run_cpu_entrypoint.sh`
4. `benchmark_scripts/check_cuda_available.py`
5. `benchmark_scripts/run_single_gpu_entrypoint.sh`
6. `benchmark_scripts/run_multi_gpu_entrypoint.sh`
7. `benchmark_scripts/measure_env_size.py`
8. `benchmark_scripts/validate_agent_report.py`
9. `benchmark_scripts/summarize_results.py`

