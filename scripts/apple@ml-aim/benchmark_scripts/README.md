# Benchmark Scripts

This repository contains an end-to-end, fully reproducible benchmark workflow under `benchmark_scripts/`.

## Quickstart

Run everything:

```bash
bash benchmark_scripts/run_all.sh
```

Key environment variables:

- `SCIMLOPSBENCH_REPORT` (default: `/opt/scimlopsbench/report.json`): agent report path
- `SCIMLOPSBENCH_PYTHON`: override python executable (highest priority for runner-based stages)
- `SCIMLOPSBENCH_OFFLINE=1`: do not attempt downloads (requires cached assets)

## Stage scripts

- `benchmark_scripts/run_pyright_missing_imports.sh`: installs/runs Pyright and reports only `reportMissingImports` diagnostics.
- `benchmark_scripts/prepare_assets.sh`: downloads minimal public dataset + model checkpoints into `benchmark_assets/`.
- `benchmark_scripts/run_cpu_entrypoint.sh`: CPU stage (skips when repo is CUDA-only by design).
- `benchmark_scripts/check_cuda_available.py`: checks CUDA availability and GPU count (exits `1` if unavailable).
- `benchmark_scripts/run_single_gpu_entrypoint.sh`: runs the official single-GPU entrypoint (1 step via 1-image dataset).
- `benchmark_scripts/run_multi_gpu_entrypoint.sh`: runs the official multi-GPU entrypoint (requires ≥2 GPUs).
- `benchmark_scripts/measure_env_size.py`: measures environment footprint from `python_path` in the agent report.
- `benchmark_scripts/validate_agent_report.py`: validates report.json and computes hallucination statistics.
- `benchmark_scripts/summarize_results.py`: aggregates per-stage results into `build_output/summary/results.json`.

## Outputs

Each stage writes:

```
build_output/<stage>/log.txt
build_output/<stage>/results.json
```

Assets are stored under:

```
benchmark_assets/cache/
benchmark_assets/dataset/
benchmark_assets/model/
```

