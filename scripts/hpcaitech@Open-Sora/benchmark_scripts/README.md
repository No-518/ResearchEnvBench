# Open-Sora Reproducible Benchmark Workflow (Scripts Only)

This directory adds a fully connected, end-to-end benchmark chain that runs:

1. Pyright missing-import check
2. Asset preparation (dataset prompt CSV + required model checkpoints)
3. Minimal CPU entrypoint run (1 step / 1 sample)
4. CUDA availability check
5. Minimal single-GPU entrypoint run (1 step / 1 sample)
6. Minimal multi-GPU entrypoint run (>=2 GPUs, 1 step / 1 sample)
7. Environment size measurement
8. Agent report validation + hallucination statistics
9. Final summary aggregation

## One-command run

From repository root:

```bash
bash benchmark_scripts/run_all.sh
```

Outputs are written under `build_output/` and assets under `benchmark_assets/`.

## Report file

Many stages resolve the Python interpreter from the agent report:

- Default: `/opt/scimlopsbench/report.json`
- Override: `SCIMLOPSBENCH_REPORT=/path/to/report.json`

The report JSON must include `"python_path": "/path/to/python"`.

## Optional authentication

If any model download requires authentication (usually not for public repos), set one of:

- `HF_TOKEN`
- `HF_AUTH_TOKEN`

## Key stage scripts

- `benchmark_scripts/run_pyright_missing_imports.sh`
- `benchmark_scripts/prepare_assets.sh`
- `benchmark_scripts/run_cpu_entrypoint.sh`
- `benchmark_scripts/check_cuda_available.py`
- `benchmark_scripts/run_single_gpu_entrypoint.sh`
- `benchmark_scripts/run_multi_gpu_entrypoint.sh`
- `benchmark_scripts/measure_env_size.py`
- `benchmark_scripts/validate_agent_report.py`
- `benchmark_scripts/summarize_results.py`

