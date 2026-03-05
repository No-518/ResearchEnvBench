# InternEvo reproducible benchmark (scripts-only)

## One-command run

```bash
bash benchmark_scripts/run_all.sh
```

This runs, in order: `pyright -> prepare -> cpu (skipped) -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary`.

## Required agent report

By default, scripts read the agent report at:

- `/opt/scimlopsbench/report.json`

Override with:

```bash
export SCIMLOPSBENCH_REPORT=/path/to/report.json
```

## Outputs

Per-stage artifacts:

- `build_output/<stage>/log.txt`
- `build_output/<stage>/results.json`

Stages: `pyright`, `prepare`, `cpu`, `cuda`, `single_gpu`, `multi_gpu`, `env_size`, `hallucination`, `summary`.

Assets:

- `benchmark_assets/cache/` (downloads + caches)
- `benchmark_assets/dataset/` (prepared dataset)
- `benchmark_assets/model/` (prepared tokenizer/model asset)

## Notes / knobs

- **Multi-GPU selection**:
  - `export SCIMLOPSBENCH_MULTI_GPU_DEVICES=0,1`
  - `export SCIMLOPSBENCH_MULTI_GPU_NPROC=2`
- **Single-/multi-GPU master ports**:
  - `export SCIMLOPSBENCH_SINGLE_GPU_PORT=29511`
  - `export SCIMLOPSBENCH_MULTI_GPU_PORT=29512`
- **Python override for training stages**:
  - `export SCIMLOPSBENCH_PYTHON=/abs/path/to/python`
  - or pass `--python /abs/path/to/python` to `run_single_gpu_entrypoint.sh` / `run_multi_gpu_entrypoint.sh`

## What’s considered “CPU” here?

InternEvo’s own installation docs (`doc/en/install.md`) specify a GPU environment; this benchmark records the CPU stage as `skipped` with `skip_reason=repo_not_supported`.

