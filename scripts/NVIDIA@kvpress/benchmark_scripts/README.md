# KVPress Benchmark Workflow (Scripts Only)

This folder contains an end-to-end, reproducible benchmark workflow that:

- Runs a Pyright missing-imports check
- Prepares assets (dataset + minimal model download)
- Runs minimal 1-sample inference via the repository entrypoint on CPU / single GPU / multi GPU
- Checks CUDA availability
- Measures environment size
- Validates the agent report and computes hallucination statistics
- Produces a final summary JSON

All outputs are written under `build_output/` and `benchmark_assets/`.

## One-Command Run

```bash
bash benchmark_scripts/run_all.sh
```

The workflow never aborts early; it runs all stages and exits `1` if any stage failed.

## Report / Python Resolution

Most stages rely on `/opt/scimlopsbench/report.json` (or overrides) to resolve the Python interpreter:

- Override report path: `SCIMLOPSBENCH_REPORT=/path/to/report.json`
- Override python path: `SCIMLOPSBENCH_PYTHON=/path/to/python`

You can also pass these to the top-level runner:

```bash
bash benchmark_scripts/run_all.sh --report-path /path/to/report.json --python /path/to/python
```

## Stage Scripts

- `benchmark_scripts/run_pyright_missing_imports.sh`
  - Writes: `build_output/pyright/{log.txt,pyright_output.json,analysis.json,results.json}`
- `benchmark_scripts/prepare_assets.sh`
  - Writes: `build_output/prepare/{log.txt,results.json}`
  - Writes: `benchmark_assets/manifest.json` and downloads into `benchmark_assets/cache/`
  - Key overrides:
    - `--dataset loogle --data-dir shortdep_qa`
    - `--model hf-internal-testing/tiny-random-LlamaForCausalLM`
    - `--offline` (requires cache to already exist)
- `benchmark_scripts/run_cpu_entrypoint.sh`
  - Uses repo entrypoint: `evaluation/evaluate.py`
  - Forces CPU via `CUDA_VISIBLE_DEVICES=""` and `--device cpu`
- `benchmark_scripts/check_cuda_available.py`
  - Uses the report’s `python_path` to probe CUDA via torch/tf/jax
- `benchmark_scripts/run_single_gpu_entrypoint.sh`
  - Forces single GPU via `CUDA_VISIBLE_DEVICES=0` and `--device cuda:0`
- `benchmark_scripts/run_multi_gpu_entrypoint.sh`
  - Requires `>=2` GPUs and uses `CUDA_VISIBLE_DEVICES=0,1` (override with `--gpus 0,1`)
  - Uses `evaluation/evaluate.py --device auto` (device_map auto)
- `benchmark_scripts/measure_env_size.py`
  - Writes `build_output/env_size/{log.txt,results.json}`
- `benchmark_scripts/validate_agent_report.py`
  - Writes `build_output/hallucination/{log.txt,results.json}`
- `benchmark_scripts/summarize_results.py`
  - Writes `build_output/summary/{log.txt,results.json}`

## Outputs

- Assets:
  - `benchmark_assets/cache/` (HF + misc caches)
  - `benchmark_assets/model/` (symlink/copy to resolved model dir)
  - `benchmark_assets/dataset/` (dataset cache file manifest)
  - `benchmark_assets/manifest.json` (single source of truth for runs)
- Stage outputs:
  - `build_output/<stage>/{log.txt,results.json}` for stages:
    `pyright, prepare, cpu, cuda, single_gpu, multi_gpu, env_size, hallucination, summary`

