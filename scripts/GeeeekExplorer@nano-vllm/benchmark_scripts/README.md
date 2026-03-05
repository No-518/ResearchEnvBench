# nano-vLLM benchmark workflow (scripts-only)

Run the full, end-to-end workflow from the repo root:

```bash
bash benchmark_scripts/run_all.sh
```

## What it does (in order)

1. `benchmark_scripts/run_pyright_missing_imports.sh` → missing-import diagnostics only
2. `benchmark_scripts/prepare_assets.sh` → creates a tiny prompts dataset and downloads `Qwen/Qwen3-0.6B` into `benchmark_assets/`
3. `benchmark_scripts/run_cpu_entrypoint.sh` → **skipped** (nano-vLLM is CUDA/NCCL-only by design)
4. `benchmark_scripts/check_cuda_available.py` → checks CUDA availability in the reported Python env
5. `benchmark_scripts/run_single_gpu_entrypoint.sh` → minimal single-GPU inference (1 prompt, `max_tokens=1`)
6. `benchmark_scripts/run_multi_gpu_entrypoint.sh` → minimal 2-GPU tensor-parallel inference (1 prompt, `max_tokens=1`)
7. `benchmark_scripts/measure_env_size.py` → measures environment footprint from `sys.prefix`
8. `benchmark_scripts/validate_agent_report.py` → validates `/opt/scimlopsbench/report.json` and writes hallucination stats
9. `benchmark_scripts/summarize_results.py` → aggregates all stage results

All artifacts are written under:
- `benchmark_assets/` (cache + dataset + model)
- `build_output/<stage>/` (logs + results)

## Key environment variables

- `SCIMLOPSBENCH_REPORT` → override report path (default `/opt/scimlopsbench/report.json`)
- `SCIMLOPSBENCH_PYTHON` → override Python interpreter (else uses `python_path` from report)
- `SCIMLOPSBENCH_MULTI_GPU_VISIBLE_DEVICES` → override multi-GPU device selection (default `0,1`)

## Offline usage

If you already downloaded assets once, you can re-run offline by passing:

```bash
bash benchmark_scripts/prepare_assets.sh --offline
```

