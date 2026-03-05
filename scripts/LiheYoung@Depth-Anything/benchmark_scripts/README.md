# Depth-Anything Benchmark Scripts

## One-command run

```bash
bash benchmark_scripts/run_all.sh
```

This runs (in order): `pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary`.

All stage artifacts are written under `build_output/<stage>/`.
Reusable assets/caches are written under `benchmark_assets/`.

## Report / python overrides

- Override agent report path:
  ```bash
  bash benchmark_scripts/run_all.sh --report-path /opt/scimlopsbench/report.json
  ```
- Override the benchmark python interpreter:
  ```bash
  bash benchmark_scripts/run_all.sh --python /opt/scimlopsbench/python
  ```

## Multi-GPU behavior

By default, `benchmark_scripts/run_multi_gpu_entrypoint.sh` is marked **skipped** (`not_applicable`) because the documented entrypoint `run.py` does not expose distributed/DDP launch options for relative-depth inference.

To attempt a real multi-GPU run, provide an explicit command:

```bash
export SCIMLOPSBENCH_MULTI_GPU_CMD='torchrun --nproc_per_node=2 <your_repo_entrypoint_and_args_here>'
bash benchmark_scripts/run_multi_gpu_entrypoint.sh
```

You can also override device visibility:

```bash
export SCIMLOPSBENCH_MULTI_GPU_CUDA_VISIBLE_DEVICES=0,1
```

