# Reproducible Benchmark Workflow (Scripts Only)

## One-command run

From the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

This runs (in order): `pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary`.

## Outputs

All outputs go to `build_output/<stage>/`:

- `log.txt` (stdout/stderr)
- `results.json` (machine-readable stage result)

Additional stage outputs:

- `build_output/pyright/pyright_output.json`, `build_output/pyright/analysis.json`

Assets are prepared under:

- `benchmark_assets/cache/` (download cache)
- `benchmark_assets/dataset/` (prepared dataset + minimal `.scp` lists)
- `benchmark_assets/model/` (resolved model directory / checkpoint)

## Agent report inputs

Default report path:

- `/opt/scimlopsbench/report.json`

Override report path:

- `SCIMLOPSBENCH_REPORT=/path/to/report.json`

Override python interpreter (highest priority for most stages):

- `SCIMLOPSBENCH_PYTHON=/path/to/python`

## Notes

- `prepare_assets.sh` downloads the minimal model checkpoint from HuggingFace when online; set `SCIMLOPSBENCH_OFFLINE=1` to force offline reuse from `benchmark_assets/cache/`.
- Multi-GPU stage requires `>=2` visible GPUs; it fails with `skip_reason="insufficient_hardware"` if not available.

