# Reproducible Benchmark Workflow

Run the full benchmark chain from the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

## Agent Report (required)

By default, stages read the agent report at:

- `/opt/scimlopsbench/report.json`

Override with:

- `SCIMLOPSBENCH_REPORT=/path/to/report.json` (env var), or
- `bash benchmark_scripts/run_all.sh --report-path /path/to/report.json`

## Python interpreter override (optional)

To force a specific Python interpreter for all stages that support it:

```bash
bash benchmark_scripts/run_all.sh --python /abs/path/to/python
```

This sets `SCIMLOPSBENCH_PYTHON` for child stages.

## Outputs

Stages write per-stage logs and results into:

- `build_output/<stage>/{log.txt,results.json}`

Additional outputs:

- `build_output/pyright/{pyright_output.json,analysis.json,results.json}`
- `benchmark_assets/{cache/,dataset/,model/}` (download/cache locations)

Final aggregation:

- `build_output/summary/results.json`

