# Benchmark scripts (torchft repo)

Run the full end-to-end workflow (does not stop on intermediate failures):

```bash
bash benchmark_scripts/run_all.sh
```

Outputs:
- Per-stage: `build_output/<stage>/{log.txt,results.json}`
- Assets: `benchmark_assets/{cache,dataset,model}/` and `benchmark_assets/manifest.json`
- Final summary: `build_output/summary/{log.txt,results.json}`

Notes:
- Entrypoint used for train/infer stages: `train_ddp.py` (README DDP example).
- Minimal run constraints are enforced via `benchmark_scripts/sitecustomize.py` by setting env vars (batch_size=1, max_steps=1, optional embedding cap).
- GPU stages require CUDA; multi-GPU requires ≥2 visible GPUs (`CUDA_VISIBLE_DEVICES` defaults to `0,1`, override via `SCIMLOPSBENCH_MULTI_GPU_DEVICES`).
- Agent report default path: `/opt/scimlopsbench/report.json` (override via `SCIMLOPSBENCH_REPORT` or `--report-path` on Python scripts that accept it).

