# FlagGems Reproducible Benchmark Workflow

Run the full pipeline from the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

Artifacts are written under `build_output/<stage>/` with one `log.txt` and one `results.json` per stage.

## Notes (FlagGems specifics)

- `cpu` stage is marked `skipped` because `flag_gems` requires an accelerator device at import time (no CPU fallback).
- `multi_gpu` stage is marked `skipped` because the repository does not ship a native distributed launch entrypoint (torchrun/accelerate/deepspeed/lightning); the docs describe multi-node usage via modifying external frameworks.

## Agent report / Python resolution

Most stages use the Python interpreter from the agent report:

- Default report path: `/opt/scimlopsbench/report.json`
- Override report path: `SCIMLOPSBENCH_REPORT=/path/to/report.json`
- Override python path: `SCIMLOPSBENCH_PYTHON=/path/to/python`

## HF downloads (prepare stage)

`prepare_assets.sh` downloads the Hugging Face tokenizer for the repo’s BERT example (`google-bert/bert-base-uncased`) into:

- Cache: `benchmark_assets/cache/`
- Resolved model link/copy: `benchmark_assets/model/`

If internet access is blocked and the cache is empty, `prepare` will fail with `download_failed`.

