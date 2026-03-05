# Reproducible Benchmark Workflow

Run the full end-to-end benchmark (does not abort early on failures):

```bash
bash benchmark_scripts/run_all.sh
```

Outputs are written under:
- `build_output/<stage>/{log.txt,results.json}` for each stage
- Prepared artifacts under `benchmark_assets/`

## Stages

- `pyright`: `bash benchmark_scripts/run_pyright_missing_imports.sh --repo .`
- `prepare`: `bash benchmark_scripts/prepare_assets.sh`
- `cpu`: `bash benchmark_scripts/run_cpu_entrypoint.sh`
- `cuda`: `python benchmark_scripts/check_cuda_available.py`
- `single_gpu`: `bash benchmark_scripts/run_single_gpu_entrypoint.sh`
- `multi_gpu`: `bash benchmark_scripts/run_multi_gpu_entrypoint.sh` (optional override: `--gpus 0,1`)
- `env_size`: `python benchmark_scripts/measure_env_size.py`
- `hallucination`: `python benchmark_scripts/validate_agent_report.py`
- `summary`: `python benchmark_scripts/summarize_results.py`

## Agent Report Resolution

Many stages prefer using the python interpreter specified by the agent report:
- Default report path: `/opt/scimlopsbench/report.json`
- Override via env: `SCIMLOPSBENCH_REPORT`
- Optional python override: `SCIMLOPSBENCH_PYTHON`

## Notes

- Model download defaults to HuggingFace `FunAudioLLM/InspireMusic-Base` (override via `HF_REPO_ID`).
- Dataset defaults to the repo-provided sample parquet under `examples/music_generation/data/samples/parquet/`.

