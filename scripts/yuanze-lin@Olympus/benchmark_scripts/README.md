# Olympus benchmark workflow (scripts-only)

This directory adds a fully connected, reproducible benchmark chain that runs:

1) Pyright missing-import diagnostics  
2) Asset preparation (dataset + model download)  
3) Minimal CPU run (repo entrypoint)  
4) CUDA availability check  
5) Minimal single-GPU run (repo entrypoint)  
6) Minimal multi-GPU run (repo entrypoint via DeepSpeed)  
7) Environment size measurement  
8) Agent report validation + hallucination statistics  
9) Final summary aggregation

All outputs are written under `build_output/` and `benchmark_assets/` (created automatically).

## One-command run

```bash
bash benchmark_scripts/run_all.sh
```

`run_all.sh` always runs all stages in order and exits `1` if any stage failed (but it never stops early).

## Required agent report

Most stages default to using the Python interpreter declared in the agent report:

- Default report path: `/opt/scimlopsbench/report.json`
- Override via env var: `SCIMLOPSBENCH_REPORT=/path/to/report.json`
- Some scripts also accept `--report-path /path/to/report.json`

The report JSON must include at least:

```json
{"python_path": "/abs/path/to/python"}
```

If the report is missing or invalid and you did not provide a `--python` override, python-dependent stages fail with `failure_category="missing_report"`.

## Stage scripts

- `benchmark_scripts/run_pyright_missing_imports.sh`: runs Pyright and reports only `reportMissingImports` diagnostics.  
- `benchmark_scripts/prepare_assets.sh`: downloads dataset/model to `benchmark_assets/cache/`, then prepares:
  - `benchmark_assets/dataset/minimal_train.json` (1-sample subset derived from downloaded `Olympus.json` with `image` removed)
  - `benchmark_assets/model/model` (symlink or copy to the downloaded model directory)
  - `benchmark_assets/manifest.json` (paths + sha256 used by later stages)
- `benchmark_scripts/run_cpu_entrypoint.sh`: runs `mipha/train/train.py` for `max_steps=1` on CPU (best-effort).  
- `benchmark_scripts/check_cuda_available.py`: checks CUDA availability (torch/tensorflow/jax) and exits `0` if available, else `1`.  
- `benchmark_scripts/run_single_gpu_entrypoint.sh`: runs `mipha/train/train.py` for `max_steps=1` with `CUDA_VISIBLE_DEVICES=0`.  
- `benchmark_scripts/run_multi_gpu_entrypoint.sh`: runs `python -m deepspeed ... mipha/train/train.py` for `max_steps=1` with `CUDA_VISIBLE_DEVICES=0,1` (fails if `<2` GPUs).  
- `benchmark_scripts/measure_env_size.py`: measures the disk footprint of `sys.prefix` and site-packages for the agent-reported python.  
- `benchmark_scripts/validate_agent_report.py`: validates `/opt/scimlopsbench/report.json` (or override) against observed stage outputs and writes hallucination stats.  
- `benchmark_scripts/summarize_results.py`: aggregates per-stage `results.json` into `build_output/summary/results.json`.

## Useful overrides

Run asset prep with explicit repos/revisions:

```bash
bash benchmark_scripts/prepare_assets.sh \
  --dataset-repo Yuanze/Olympus --dataset-rev main \
  --model-repo zhumj34/Mipha-3B --model-rev main
```

Provide tokens for gated Hugging Face downloads (if required by a chosen repo):

```bash
export HF_TOKEN=...
```

Force a specific python (highest priority for scripts that accept it):

```bash
bash benchmark_scripts/prepare_assets.sh --python /abs/path/to/python
bash benchmark_scripts/run_cpu_entrypoint.sh --python /abs/path/to/python
```
