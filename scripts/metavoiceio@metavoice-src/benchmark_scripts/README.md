# Reproducible Benchmark Workflow

Run the full end-to-end benchmark from the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

This will execute, in order:
`pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary`

## Outputs

All outputs are written under:

- `benchmark_assets/` (dataset/model/cache)
- `build_output/<stage>/` (logs + per-stage `results.json`)

The final aggregated summary is:

- `build_output/summary/results.json`

## Report JSON

By default, scripts read the agent report from:

- `/opt/scimlopsbench/report.json`

Override via:

- `SCIMLOPSBENCH_REPORT=/path/to/report.json` (environment)
- `--report-path /path/to/report.json` (when supported)

## Optional Overrides

- Use a specific Python executable for stages that accept it:
  - `--python /abs/path/to/python`

