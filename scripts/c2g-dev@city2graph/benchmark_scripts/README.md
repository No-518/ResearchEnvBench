# Reproducible Benchmark Workflow

Run the full workflow from the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

Outputs are written under `build_output/`.

## Required Agent Report

These scripts expect the environment report at:

- Default: `/opt/scimlopsbench/report.json`
- Override: `SCIMLOPSBENCH_REPORT=/path/to/report.json`

Key fields used: `python_path`, `python_version`, `torch_version`, `cuda_available`, `gpu_count`, `ddp_expected_ok`.

## Entrypoint Stages (CPU / Single GPU / Multi GPU)

This repository appears to be a library (no train/infer CLI entrypoint). The entrypoint stages therefore default to `status="skipped"` unless you provide an official repo-native command.

Provide commands via environment variables (optional `{python}` placeholder is replaced with `python_path` from the report):

```bash
export SCIMLOPSBENCH_CPU_COMMAND='{python} -m pytest -q tests/test_graph.py::TestGraphConversion::test_gdf_to_pyg_basic'
export SCIMLOPSBENCH_SINGLE_GPU_COMMAND='CUDA_VISIBLE_DEVICES=0 {python} your_entrypoint.py --steps 1 --batch_size 1'
export SCIMLOPSBENCH_MULTI_GPU_COMMAND='torchrun --nproc_per_node=2 {python} your_entrypoint.py --steps 1 --batch_size 1'
export SCIMLOPSBENCH_MULTI_GPU_DEVICES='0,1'
```

If you do not set these, the scripts will record a reviewable skip reason and continue.

## Assets

`prepare_assets.sh` prepares:

- Dataset: copies bundled sample GeoJSON files from `tests/data/` into `benchmark_assets/dataset/` (with sha256 manifest in `benchmark_assets/cache/`).
- Model: creates a small placeholder under `benchmark_assets/model/` (this repo does not ship/require weights).
