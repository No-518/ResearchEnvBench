# Reproducible Benchmark Workflow

Run everything (no early aborts):

```bash
bash benchmark_scripts/run_all.sh
```

Common overrides:

```bash
# Use a non-default agent report location
bash benchmark_scripts/run_all.sh --report-path /opt/scimlopsbench/report.json

# Force a specific Python for stages that accept it
bash benchmark_scripts/run_all.sh --python /opt/scimlopsbench/python

# Pick a different HF model and/or prompt for the minimal run
bash benchmark_scripts/run_all.sh --model-id Tongyi-MAI/Z-Image-Turbo --model-revision main --prompt "a cup of coffee on the table"

# Override the multi-GPU visible set (default: 0,1)
bash benchmark_scripts/run_all.sh --cuda-visible-devices 0,1
```

Notes:
- `prepare_assets.sh` downloads the model into `benchmark_assets/cache/` and links it into `benchmark_assets/model/`. If Hugging Face auth is required, set `HF_AUTH_TOKEN` (or the script will prompt when run interactively).
- To reuse cached assets without network, set `SCIMLOPSBENCH_OFFLINE=1`.
- All stage outputs are written under `build_output/<stage>/{log.txt,results.json}`.

