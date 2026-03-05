# VisionMamba reproducible benchmark scripts

These scripts add an end-to-end, reproducible benchmark workflow to this repository **without modifying any existing repo files**.

## Quickstart

Run the full workflow from the repo root:

```bash
bash benchmark_scripts/run_all.sh
```

Override the agent report location:

```bash
bash benchmark_scripts/run_all.sh --report-path /opt/scimlopsbench/report.json
```

## Inputs

- Agent report (default): `/opt/scimlopsbench/report.json`
  - Override via env var: `SCIMLOPSBENCH_REPORT`
  - Or via `run_all.sh --report-path ...`
- Optional python override (highest priority after per-script `--python` flags):
  - `SCIMLOPSBENCH_PYTHON=/abs/path/to/python`

## Assets

`prepare_assets.sh` writes assets under:

- `benchmark_assets/cache/`
- `benchmark_assets/dataset/`
- `benchmark_assets/model/`
- `benchmark_assets/assets.json` (manifest used by run stages)

Optional asset overrides:

- `SCIMLOPSBENCH_DATASET_URL` (anonymous URL for a tiny dataset file)
- `SCIMLOPSBENCH_MODEL_URL` (anonymous URL for a tiny model artifact)

## Outputs

Each stage writes:

- `build_output/<stage>/log.txt`
- `build_output/<stage>/results.json`

Stages:
`pyright`, `prepare`, `cpu`, `cuda`, `single_gpu`, `multi_gpu`, `env_size`, `hallucination`, `summary`.

