# Reproducible Benchmark Workflow (Scripts Only)

## One-command run

```bash
bash benchmark_scripts/run_all.sh
```

This executes, in order:
`pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary`

All per-stage outputs are written under `build_output/<stage>/` and assets under `benchmark_assets/`.

## Notes / Overrides

- Report path override (used by python-resolving scripts):
  - `export SCIMLOPSBENCH_REPORT=/path/to/report.json`
  - or pass `--report-path ...` to individual stage scripts
- Explicit python override for stages that support it:
  - `bash benchmark_scripts/prepare_assets.sh --python /abs/path/to/python`
  - `bash benchmark_scripts/run_cpu_entrypoint.sh --python /abs/path/to/python`
- Multi-GPU device override:
  - `export SCIMLOPSBENCH_MULTI_GPU_DEVICES="0,1"`
  - `bash benchmark_scripts/run_multi_gpu_entrypoint.sh --devices "0,1" --nproc 2`

## Where to look

- Stage logs: `build_output/<stage>/log.txt`
- Stage results: `build_output/<stage>/results.json`
- Final summary: `build_output/summary/results.json`

