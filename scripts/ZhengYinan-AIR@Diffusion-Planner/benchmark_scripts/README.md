# Diffusion-Planner: Reproducible Benchmark Scripts

This folder adds a fully connected, end-to-end benchmark workflow **without modifying any repository files**.

## One-command Run

From the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

## Required Agent Report

Most stages resolve the benchmarked Python interpreter from the agent report:

- default: `/opt/scimlopsbench/report.json`
- override:
  - env: `SCIMLOPSBENCH_REPORT=/path/to/report.json`
  - CLI: `bash benchmark_scripts/run_all.sh --report-path /path/to/report.json`

To override the python executable directly:

```bash
bash benchmark_scripts/run_all.sh --python /abs/path/to/python
```

## What It Does (Stages)

1. `pyright`: installs/runs Pyright and counts only `reportMissingImports` diagnostics.
2. `prepare`: generates a tiny synthetic dataset in the repo’s expected `.npz` format and downloads the public HF checkpoint files referenced in `README.md`.
3. `cpu`: runs `train_predictor.py` for 1 epoch on CPU (`CUDA_VISIBLE_DEVICES=""`, `--device cpu`, `--ddp false`).
4. `cuda`: probes CUDA availability under the reported Python (torch/tensorflow/jax).
5. `single_gpu`: runs `train_predictor.py` for 1 epoch with `CUDA_VISIBLE_DEVICES=0`.
6. `multi_gpu`: runs DDP via `python -m torch.distributed.run` with `CUDA_VISIBLE_DEVICES=0,1` (skips if `<2` GPUs detected).
7. `env_size`: measures `sys.prefix` size and site-packages sizes for the reported Python.
8. `hallucination`: validates `report.json` fields and computes path/version/capability hallucination stats using observed stage results.
9. `summary`: aggregates per-stage results into `build_output/summary/results.json`.

## Outputs

Artifacts are written only to these new directories:

- `benchmark_assets/` (dataset/model/cache)
- `build_output/<stage>/{log.txt,results.json}`

## Notes

- `prepare_assets.sh` uses the Hugging Face URLs from the project README; if network is restricted/offline and the cache is empty, the `prepare` stage fails with `download_failed`.
- The minimal synthetic dataset is intentionally tiny (1 sample) to make `train_predictor.py` run quickly.
- The repo’s LR scheduler asserts `train_epochs >= warm_up_epoch`; the benchmark sets `--train_epochs 1 --warm_up_epoch 1` to enable a true 1-step smoke run.

