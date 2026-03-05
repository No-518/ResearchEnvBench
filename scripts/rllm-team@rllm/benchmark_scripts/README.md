# Reproducible Benchmark Workflow (Scripts Only)

Run the full pipeline from the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

## Report / Python Resolution

- Default report path: `/opt/scimlopsbench/report.json`
- Override report path:
  - `SCIMLOPSBENCH_REPORT=/path/to/report.json bash benchmark_scripts/run_all.sh`
  - or `bash benchmark_scripts/run_all.sh --report-path /path/to/report.json`
- Override python:
  - `SCIMLOPSBENCH_PYTHON=/path/to/python bash benchmark_scripts/run_all.sh`
  - or `bash benchmark_scripts/run_all.sh --python /path/to/python`

## What It Produces

Artifacts are written only under:

- `benchmark_assets/` (downloads/caches)
- `build_output/` (logs/results)

Per-stage outputs:

- `build_output/pyright/{log.txt,pyright_output.json,analysis.json,results.json}`
- `build_output/prepare/{log.txt,results.json}`
- `build_output/cpu/{log.txt,results.json}`
- `build_output/cuda/{log.txt,results.json}`
- `build_output/single_gpu/{log.txt,results.json}`
- `build_output/multi_gpu/{log.txt,results.json}` (skipped)
- `build_output/env_size/{log.txt,results.json}`
- `build_output/hallucination/{log.txt,results.json}`
- `build_output/summary/{log.txt,results.json}`

## Entrypoints Used

- Training benchmark uses `examples/rdl.py` on RelBench Rel-F1 (`--task driver-position`) with:
  - `epochs=1`, `batch_size=1`, `max_steps_per_epoch=1`
  - CPU is forced via `CUDA_VISIBLE_DEVICES=""`
  - Single GPU is forced via `CUDA_VISIBLE_DEVICES=0`
  - Note: `--task driver-position` is used in scripts to avoid `BatchNorm1d(out_dim=1)` failing at `batch_size=1`.
- Multi-GPU stage is marked `skipped` with `skip_reason="not_applicable"` (user requested multi_gpu skipped).
