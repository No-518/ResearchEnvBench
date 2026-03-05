# Auto1111SDK Benchmark Scripts

One command end-to-end run:

```bash
bash benchmark_scripts/run_all.sh
```

## What it runs (in order)

1. `benchmark_scripts/run_pyright_missing_imports.sh` (missing-import diagnostics only)
2. `benchmark_scripts/prepare_assets.sh` (MNIST download + toy model config copy)
3. `benchmark_scripts/run_cpu_entrypoint.sh` (1 training step, CPU)
4. `benchmark_scripts/check_cuda_available.py` (CUDA + GPU count probe)
5. `benchmark_scripts/run_single_gpu_entrypoint.sh` (1 training step, single GPU)
6. `benchmark_scripts/run_multi_gpu_entrypoint.sh` (1 training step, 2 GPUs)
7. `benchmark_scripts/measure_env_size.py` (environment footprint)
8. `benchmark_scripts/validate_agent_report.py` (report validation + hallucination statistics)
9. `benchmark_scripts/summarize_results.py` (final summary)

## Outputs

Per-stage outputs land under `build_output/<stage>/`:

- `log.txt`
- `results.json`

Assets are stored under `benchmark_assets/{cache,dataset,model}/`.

## Entrypoint used for runs

The CPU/GPU runs use the repository’s training entrypoint:

- `auto1111sdk/modules/generative/main.py`

With the prepared config:

- `benchmark_assets/model/mnist_toy/mnist.yaml`

And dataset directory:

- `benchmark_assets/dataset/mnist/`

## Optional overrides

If `/opt/scimlopsbench/report.json` is unavailable, you can pass an explicit Python to stages that support it:

```bash
bash benchmark_scripts/prepare_assets.sh --python /path/to/python
bash benchmark_scripts/run_cpu_entrypoint.sh --python /path/to/python
bash benchmark_scripts/run_single_gpu_entrypoint.sh --python /path/to/python
bash benchmark_scripts/run_multi_gpu_entrypoint.sh --python /path/to/python
```

Multi-GPU device selection:

```bash
bash benchmark_scripts/run_multi_gpu_entrypoint.sh --cuda-visible-devices 0,1
```
