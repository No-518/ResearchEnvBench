# RenderFormer Benchmark Scripts

Run the full end-to-end benchmark workflow from the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

## What it does (in order)

1. Pyright missing-import detection (`build_output/pyright/`)
2. Asset preparation: example scene → HDF5 + Hugging Face model download (`build_output/prepare/`, `benchmark_assets/`)
3. Minimal CPU inference via `infer.py` (`build_output/cpu/`)
4. CUDA availability check (uses `python_path` from report) (`build_output/cuda/`)
5. Minimal single-GPU inference via `infer.py` (`build_output/single_gpu/`)
6. Minimal multi-GPU inference via `torchrun` + `infer.py` (`build_output/multi_gpu/`)
7. Environment size measurement (uses `python_path` from report) (`build_output/env_size/`)
8. Agent report validation + hallucination stats (`build_output/hallucination/`)
9. Final summary (`build_output/summary/`)

## Required agent report

By default, scripts read the agent report at:

`/opt/scimlopsbench/report.json`

Override with:

- `SCIMLOPSBENCH_REPORT=/path/to/report.json`

The report must contain `python_path` pointing to the environment to benchmark.

## Useful environment variables

- `SCIMLOPSBENCH_REPORT`: override report path
- `SCIMLOPSBENCH_PYTHON`: override python executable for stages that use `runner.py`
- `SCIMLOPSBENCH_MULTI_GPU_VISIBLE_DEVICES`: override multi-GPU device list (default: `0,1`)

## Assets & cache locations

All downloads/caches are kept under:

- `benchmark_assets/cache/` (HF cache, pip cache, imageio cache, tmp)
- `benchmark_assets/dataset/` (generated `.h5` from `examples/*.json`)
- `benchmark_assets/model/` (symlink to the resolved local HF snapshot directory)

If offline, reruns will reuse cached assets when present.

