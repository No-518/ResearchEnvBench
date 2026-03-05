# Benchmark Scripts (Chitu)

One command end-to-end workflow:

```bash
bash benchmark_scripts/run_all.sh
```

## What it runs

Stages (in order): `pyright` → `prepare` → `cpu` → `cuda` → `single_gpu` → `multi_gpu` → `env_size` → `hallucination` → `summary`.

Outputs are written under:
- `build_output/<stage>/{log.txt,results.json}`
- `benchmark_assets/{cache,dataset,model}`

## Defaults / Overrides

### Report / Python

By default scripts read `/opt/scimlopsbench/report.json` (or `$SCIMLOPSBENCH_REPORT`) and use its `python_path`. You can override:

```bash
export SCIMLOPSBENCH_REPORT=/path/to/report.json
export SCIMLOPSBENCH_PYTHON=/path/to/python
```

### Model (HuggingFace)

Defaults:
- `SCIMLOPSBENCH_MODEL_NAME=Qwen2.5-0.5B` (Chitu model config name)
- `SCIMLOPSBENCH_MODEL_REPO_ID=Qwen/Qwen2.5-0.5B`

Override example:

```bash
export SCIMLOPSBENCH_MODEL_NAME=Qwen2.5-0.5B-Instruct
export SCIMLOPSBENCH_MODEL_REPO_ID=Qwen/Qwen2.5-0.5B-Instruct
```

If the model is gated, set `HF_TOKEN`/`HUGGINGFACE_HUB_TOKEN` in your environment.

### Dataset

Default dataset source is Tiny Shakespeare (downloaded from GitHub) and converted into a ShareGPT-like JSON file.

Override example:

```bash
export SCIMLOPSBENCH_DATASET_SOURCE_URL=https://example.com/some.txt
export SCIMLOPSBENCH_DATASET_NAME=my_sharegpt_dataset
export SCIMLOPSBENCH_DATASET_NUM_SAMPLES=64
```

### Multi-GPU selection

```bash
export SCIMLOPSBENCH_MULTI_GPU_IDS=0,1
```

## Notes

- The `cpu` stage is marked `skipped` because `docs/zh/FAQ.md` states pure CPU inference is not supported yet (CPU+GPU hybrid only).
- Hydra outputs are redirected under `build_output/<stage>/hydra` to avoid writing outside benchmark directories.

