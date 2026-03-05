# Depth-Anything-V2 Reproducible Benchmark Scripts

These scripts add an end-to-end, repeatable benchmark workflow to this repository **without modifying any existing repo files**.

## Quickstart

Run the full workflow from the repo root:

```bash
bash benchmark_scripts/run_all.sh
```

Optional: override the agent report path (default: `/opt/scimlopsbench/report.json`):

```bash
bash benchmark_scripts/run_all.sh --report-path /opt/scimlopsbench/report.json
```

## What It Does (Stage Order)

1. `pyright`: Installs/runs Pyright and reports only `reportMissingImports`.
2. `prepare`: Prepares a 1-image dataset and downloads a minimal public checkpoint.
3. `cpu`: Runs a minimal **CPU** inference using the repo entrypoint `metric_depth/run.py` (1 image).
4. `cuda`: Checks CUDA availability (expects to be run under the report `python_path`).
5. `single_gpu`: Runs a minimal **single-GPU** inference using `metric_depth/run.py` (1 image).
6. `multi_gpu`: Marked **skipped/not_applicable** for this inference-focused workflow (no distributed inference entrypoint exposed for `metric_depth/run.py`).
7. `env_size`: Measures environment size rooted at `sys.prefix` of `python_path` from the report.
8. `hallucination`: Validates `/opt/scimlopsbench/report.json` against observed results and counts hallucinations.
9. `summary`: Aggregates all stage results into `build_output/summary/results.json`.

## Outputs

All outputs are written under:

```
build_output/<stage>/{log.txt,results.json}
```

Asset cache and prepared inputs are written under:

```
benchmark_assets/{cache,dataset,model}/
```

## Notes / Customization

- `prepare` defaults:
  - Dataset: `assets/examples/demo01.jpg` copied to `benchmark_assets/dataset/images/`
  - Model: public HF checkpoint (metric-depth small, hypersim) downloaded into `benchmark_assets/cache/model/`
- To override the dataset image or model URL:
  - `bash benchmark_scripts/prepare_assets.sh --dataset-image <path> --model-url <url>`
- If the model download cannot be performed (offline), `prepare` will reuse the cached model if present.

