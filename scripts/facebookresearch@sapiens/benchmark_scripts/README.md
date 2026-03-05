# Reproducible Benchmark Workflow (Scripts Only)

Run the full benchmark chain from the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

## Inputs

- Agent report (default): `/opt/scimlopsbench/report.json`
  - Override: `SCIMLOPSBENCH_REPORT=/path/to/report.json`
- Force the benchmarked Python executable (highest priority for stages that use `runner.py`):
  - `SCIMLOPSBENCH_PYTHON=/abs/path/to/python`

## Downloads / Offline

`benchmark_scripts/prepare_assets.sh` downloads:
- Dataset: `facebook/sapiens_toy_dataset` (Hugging Face dataset)
- Model checkpoint: pretrain checkpoint (`sapiens_0.3b_epoch_1600_clean.pth`) from `facebook/sapiens-pretrain-0.3b`

It uses `benchmark_assets/cache/` for caches and supports offline reuse if the cache already exists.

## GPU Controls

- Single GPU stage forces: `CUDA_VISIBLE_DEVICES=0`
- Multi GPU stage forces: `CUDA_VISIBLE_DEVICES=0,1`
  - Override visible GPUs: `SCIMLOPSBENCH_MULTI_GPU_DEVICES=0,1`
  - Override torchrun master port: `SCIMLOPSBENCH_MULTI_GPU_MASTER_PORT=29500`

## Outputs

Per-stage outputs are written to:
- `build_output/<stage>/log.txt`
- `build_output/<stage>/results.json`

Stages:
- `pyright`, `prepare`, `cpu`, `cuda`, `single_gpu`, `multi_gpu`, `env_size`, `hallucination`, `summary`

Downloaded assets live in:
- `benchmark_assets/dataset/`
- `benchmark_assets/model/`
