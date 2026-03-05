# Benchmark Scripts

Run the full end-to-end workflow from the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

Optional overrides:

```bash
# Use a non-default report.json location
bash benchmark_scripts/run_all.sh --report-path /opt/scimlopsbench/report.json

# Force a specific Python interpreter for all stages
bash benchmark_scripts/run_all.sh --python /opt/scimlopsbench/python
```

Outputs are written under `build_output/<stage>/` for:
`pyright`, `prepare`, `cpu`, `cuda`, `single_gpu`, `multi_gpu`, `env_size`, `hallucination`, `summary`.

Assets are prepared under `benchmark_assets/{cache,dataset,model}/`.

