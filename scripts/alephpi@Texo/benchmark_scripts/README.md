# Texo Benchmark Scripts

All scripts are additive-only and write exclusively to:

- `benchmark_assets/` (cache + prepared assets)
- `build_output/` (per-stage logs/results)

## One-command run

From the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

## Report / Python resolution

Stages default to reading the agent report at:

- `/opt/scimlopsbench/report.json`

Overrides:

- `SCIMLOPSBENCH_REPORT=/path/to/report.json`
- `SCIMLOPSBENCH_PYTHON=/path/to/python`

## Outputs

Each stage writes:

- `build_output/<stage>/log.txt`
- `build_output/<stage>/results.json`

Additional stage outputs:

- `build_output/pyright/pyright_output.json`
- `build_output/pyright/analysis.json`

Final summary:

- `build_output/summary/results.json`

