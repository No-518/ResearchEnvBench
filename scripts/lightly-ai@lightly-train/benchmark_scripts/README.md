# Benchmark Scripts

This folder contains a fully-connected, end-to-end benchmark workflow for this repo.

## Quick start

```bash
bash benchmark_scripts/run_all.sh
```

Outputs are written under `build_output/` and assets/caches under `benchmark_assets/`.

## Overrides

- Override agent report path:
  - `SCIMLOPSBENCH_REPORT=/path/to/report.json bash benchmark_scripts/run_all.sh`
  - or `bash benchmark_scripts/run_all.sh --report-path /path/to/report.json`
- Override python interpreter for all stages:
  - `SCIMLOPSBENCH_PYTHON=/path/to/python bash benchmark_scripts/run_all.sh`
  - or `bash benchmark_scripts/run_all.sh --python /path/to/python`

## Stages

Executed in order by `benchmark_scripts/run_all.sh`:

1. `run_pyright_missing_imports.sh` (missing-import diagnostics only)
2. `prepare_assets.sh` (downloads `coco128_unlabeled` + DINOv3 `vitt16` weights)
3. `run_cpu_entrypoint.sh` (1-step pretrain on CPU via LightlyTrain CLI entrypoint)
4. `check_cuda_available.py` (CUDA availability + GPU count)
5. `run_single_gpu_entrypoint.sh` (1-step pretrain on single GPU if available)
6. `run_multi_gpu_entrypoint.sh` (1-step pretrain on 2 GPUs; fails if <2 GPUs)
7. `measure_env_size.py` (environment footprint based on `sys.prefix`)
8. `validate_agent_report.py` (path/version/capability hallucination stats)
9. `summarize_results.py` (aggregated summary)

## Notes

- GPU stages are forced via `CUDA_VISIBLE_DEVICES` and LightlyTrain CLI args (`accelerator`, `devices`).
- Network access is required for the first run of `prepare_assets.sh` unless `benchmark_assets/cache/` already contains the needed files.

