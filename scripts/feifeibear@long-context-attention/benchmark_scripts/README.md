# Reproducible Benchmark Workflow (Scripts Only)

This directory contains an end-to-end benchmark/validation chain for this repository.

## One-command run

From the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

Outputs are written under `build_output/<stage>/` with per-stage `log.txt` and `results.json`.

## Report / Python resolution

The benchmark prefers the agent report at:

- Default: `/opt/scimlopsbench/report.json`
- Override: `SCIMLOPSBENCH_REPORT=/path/to/report.json`

Most stages use the report’s `python_path`. You can override python with:

```bash
export SCIMLOPSBENCH_PYTHON=/path/to/python
```

## Notes specific to this repo

- `prepare_assets.sh` is **skipped** as `not_applicable` because the native benchmark entrypoints generate synthetic QKV tensors (no external dataset/model checkpoint required).
- `run_cpu_entrypoint.sh` is **skipped** as `repo_not_supported` because the native entrypoints initialize NCCL and select CUDA devices unconditionally (no CPU mode exposed).

## GPU stage configuration

- Single GPU forces `CUDA_VISIBLE_DEVICES=0`.
  - Override master port: `SCIMLOPSBENCH_SINGLE_GPU_MASTER_PORT=29511`
- Multi GPU defaults to `CUDA_VISIBLE_DEVICES=0,1` and `--nproc_per_node=2`.
  - Override visible devices: `SCIMLOPSBENCH_MULTI_GPU_VISIBLE_DEVICES=0,1`
  - Override nproc: `SCIMLOPSBENCH_MULTI_GPU_NPROC=2`
  - Override master port: `SCIMLOPSBENCH_MULTI_GPU_MASTER_PORT=29521`

## Stage list / outputs

Stages executed in order:

1. `pyright` → `build_output/pyright/`
2. `prepare` → `build_output/prepare/`
3. `cpu` → `build_output/cpu/`
4. `cuda` → `build_output/cuda/`
5. `single_gpu` → `build_output/single_gpu/`
6. `multi_gpu` → `build_output/multi_gpu/`
7. `env_size` → `build_output/env_size/`
8. `hallucination` → `build_output/hallucination/`
9. `summary` → `build_output/summary/`

