# LettuceDetect Benchmark Scripts

One-command workflow (runs all stages and never aborts early):

```bash
bash benchmark_scripts/run_all.sh
```

## Outputs

All artifacts are written under:

- `build_output/<stage>/{log.txt,results.json}`
- `benchmark_assets/{cache,dataset,model}/` (download/cache locations)

Stages (in order): `pyright`, `prepare`, `cpu`, `cuda`, `single_gpu`, `multi_gpu`, `env_size`, `hallucination`, `summary`.

## Key environment variables

- `SCIMLOPSBENCH_REPORT` (optional): path to report JSON (default: `/opt/scimlopsbench/report.json`)
- `SCIMLOPSBENCH_PYTHON` (optional): override python executable used by stages that rely on the report
- `SCIMLOPSBENCH_OFFLINE=1` (optional): avoid network where supported by the stage scripts (recommended in restricted environments)
- `HF_TOKEN` / `HUGGINGFACE_HUB_TOKEN` (optional): Hugging Face token if you download from gated repos

## Stage commands (manual)

- Pyright missing imports:
  - `bash benchmark_scripts/run_pyright_missing_imports.sh --repo .`
- Prepare dataset + model:
  - `bash benchmark_scripts/prepare_assets.sh`
- CPU 1-step train:
  - `bash benchmark_scripts/run_cpu_entrypoint.sh`
- CUDA availability:
  - `python benchmark_scripts/check_cuda_available.py`
- Single-GPU 1-step train:
  - `bash benchmark_scripts/run_single_gpu_entrypoint.sh`
- Multi-GPU:
  - `bash benchmark_scripts/run_multi_gpu_entrypoint.sh`
- Environment size:
  - `python benchmark_scripts/measure_env_size.py`
- Agent report validation + hallucination stats:
  - `python benchmark_scripts/validate_agent_report.py`
- Final summary:
  - `python benchmark_scripts/summarize_results.py`

