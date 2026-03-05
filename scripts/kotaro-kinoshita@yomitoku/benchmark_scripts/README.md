# Reproducible Benchmark Workflow

This folder contains an end-to-end benchmark script chain for this repository:

- Pyright missing-import detection
- Asset preparation (dataset + minimal model snapshots)
- Minimal CPU inference (repo entrypoint)
- CUDA availability check
- Minimal single-GPU inference (repo entrypoint)
- Minimal multi-GPU stage (skipped if repo lacks distributed entrypoint)
- Environment size measurement
- Agent report validation + hallucination statistics
- Final summary aggregation

## One-command run

```bash
bash benchmark_scripts/run_all.sh
```

### Optional overrides

- Override report path (default: `/opt/scimlopsbench/report.json`):

```bash
bash benchmark_scripts/run_all.sh --report-path /path/to/report.json
```

- Override python used for stages that accept it:

```bash
bash benchmark_scripts/run_all.sh --python /abs/path/to/python
```

## Outputs

All outputs are written under:

- `benchmark_assets/` (download cache, dataset, model snapshots)
- `build_output/<stage>/` for each stage:
  - `log.txt`
  - `results.json`

Final summary:

- `build_output/summary/results.json`

