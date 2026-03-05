# Benchmark Scripts

One-command workflow (runs all stages and never aborts early):

```bash
bash benchmark_scripts/run_all.sh
```

## Agent Report

By default, scripts read the agent report from:

`/opt/scimlopsbench/report.json`

Override with:

- `--report-path /path/to/report.json` (preferred)
- or `SCIMLOPSBENCH_REPORT=/path/to/report.json`

Override the python used for python-based stages with:

- `--python /path/to/python`
- or `SCIMLOPSBENCH_PYTHON=/path/to/python`

## Entrypoint Runs (CPU / Single-GPU / Multi-GPU)

This repo is GUI-centric (`comic.py`), but it includes a repo-native inference component we can run headlessly:

- `modules/detection/rtdetr_v2_onnx.py` (RT-DETR-v2 ONNX text/bubble detector)

The benchmark uses that detector to perform **one inference step on one prepared image** for:

- CPU stage (`device=cpu`, `CUDA_VISIBLE_DEVICES=""`)
- Single-GPU stage (`device=cuda`, `CUDA_VISIBLE_DEVICES=0`, requires `onnxruntime-gpu`)

Note: The detector code uses `huggingface_hub.hf_hub_download()` by default. The benchmark stages patch it at runtime to read the prepared local files under `benchmark_assets/model/...` so runs work with `HF_HUB_OFFLINE=1` and do not require network access.

### Multi-GPU

This repo does not expose an official distributed multi-GPU entrypoint (no `torchrun` / `accelerate launch` / `deepspeed` / `lightning` commands in docs/code), so `run_multi_gpu_entrypoint.sh` defaults to `status="skipped"` with `skip_reason="repo_not_supported"`.

If you *do* have a repo-native distributed command, you can override it.

Provide commands via environment variables:

- `SCIMLOPSBENCH_CPU_COMMAND`
- `SCIMLOPSBENCH_SINGLE_GPU_COMMAND`
- `SCIMLOPSBENCH_MULTI_GPU_COMMAND`

Optional device selection env vars:

- `SCIMLOPSBENCH_SINGLE_GPU_DEVICES` (default `0`)
- `SCIMLOPSBENCH_MULTI_GPU_DEVICES` (default `0,1`)

Each command should be a full repo-native invocation that performs exactly one minimal unit of work (e.g., one image / one step) and uses the assets prepared under `benchmark_assets/`.

## Outputs

All outputs are written under:

- `benchmark_assets/` (cache, dataset, model)
- `build_output/<stage>/` (log.txt + results.json per stage)
