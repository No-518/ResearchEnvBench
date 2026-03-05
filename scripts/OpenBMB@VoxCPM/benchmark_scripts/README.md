# VoxCPM Reproducible Benchmark Workflow

This directory contains **scripts only** to run an end-to-end benchmark on the current repository:

`pyright → prepare assets → CPU run → CUDA check → single‑GPU run → multi‑GPU run → env size → agent report validation → summary`

## One-command run

```bash
bash benchmark_scripts/run_all.sh
```

All outputs are written under:

- `benchmark_assets/` (download/cache + prepared dataset/model)
- `build_output/` (per-stage logs + results)

## Important inputs

- Agent report (required for most stages):
  - Default: `/opt/scimlopsbench/report.json`
  - Override: `SCIMLOPSBENCH_REPORT=/path/to/report.json`
- Python interpreter selection:
  - `run_all.sh` will try to set `SCIMLOPSBENCH_PYTHON` from the report’s `python_path`.
  - You can override explicitly: `SCIMLOPSBENCH_PYTHON=/path/to/python`

## Model/dataset defaults

`prepare_assets.sh` follows the repo docs/examples:

- Dataset: uses the repo-provided `examples/example.wav` and writes a 1-line JSONL manifest to `benchmark_assets/dataset/train_manifest.jsonl`.
- Model: downloads the smaller official weights by default:
  - Hugging Face: `openbmb/VoxCPM-0.5B`

Overrides:

```bash
VOXCPM_MODEL_ID=openbmb/VoxCPM1.5 bash benchmark_scripts/prepare_assets.sh
VOXCPM_MODEL_REVISION=<hf_commit_or_tag> bash benchmark_scripts/prepare_assets.sh
```

If a model requires auth, set `HF_TOKEN` or `HUGGINGFACE_HUB_TOKEN`.

## Multi-GPU selection

By default multi-GPU uses `CUDA_VISIBLE_DEVICES=0,1` and launches with:

`python -m torch.distributed.run --nproc_per_node=2 scripts/train_voxcpm_finetune.py ...`

Override visible GPUs:

```bash
bash benchmark_scripts/run_multi_gpu_entrypoint.sh --gpus 0,1
```

## Outputs

Each stage writes (even on failure):

- `build_output/<stage>/log.txt`
- `build_output/<stage>/results.json`

Final summary:

- `build_output/summary/results.json`

