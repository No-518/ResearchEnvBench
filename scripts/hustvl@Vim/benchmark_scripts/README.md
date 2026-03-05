# Reproducible Benchmark Workflow (Scripts Only)

This folder contains an end-to-end, reproducible benchmark chain for this repository:

`pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary`

## Quickstart

Run everything from the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

Outputs are written to:
- `build_output/<stage>/{log.txt,results.json}` for each stage
- `benchmark_assets/{cache,dataset,model}` for downloaded assets

## What Gets Benchmarked

The default minimal run uses Detectron2’s official entrypoint:
- `det/tools/plain_train_net.py` (1 iteration: `SOLVER.MAX_ITER=1`, `SOLVER.IMS_PER_BATCH=1`)
- Dataset: Detectron2 mini COCO `val2017_100` (public download)
- Model weights: Detectron2 MSRA ResNet-50 backbone (`R-50.pkl`, public download)

## Key Environment Overrides

- `SCIMLOPSBENCH_REPORT`: override agent report path (default: `/opt/scimlopsbench/report.json`)
- `SCIMLOPSBENCH_PYTHON`: override python used for repository entrypoints (otherwise read from report.json)
- `SCIMLOPSBENCH_MULTI_GPU_DEVICES`: override multi-GPU device list (default: `0,1`)

Timeouts (seconds):
- `SCIMLOPSBENCH_PREPARE_TIMEOUT_SEC` (default 1200)
- `SCIMLOPSBENCH_CPU_TIMEOUT_SEC` (default 600)
- `SCIMLOPSBENCH_SINGLE_GPU_TIMEOUT_SEC` (default 600)
- `SCIMLOPSBENCH_MULTI_GPU_TIMEOUT_SEC` (default 1200)

