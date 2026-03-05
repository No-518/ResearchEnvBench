# SocialED Reproducible Benchmark (Scripts-Only)

## One-command run

```bash
bash benchmark_scripts/run_all.sh
```

Outputs are written under `build_output/<stage>/{log.txt,results.json}`.

## Key inputs

- Agent report (python resolution): default `/opt/scimlopsbench/report.json`
  - Override: `SCIMLOPSBENCH_REPORT=/path/to/report.json`
- Asset cache + prepared assets:
  - Cache: `benchmark_assets/cache/`
  - Dataset: `benchmark_assets/dataset/`
  - Model: `benchmark_assets/model/`

## Asset preparation overrides

```bash
SCIMLOPSBENCH_DATASET=Event2012 \
SCIMLOPSBENCH_DATASET_REPO=https://github.com/ChenBeici/SocialED_datasets.git \
SCIMLOPSBENCH_MODEL_ID=sentence-transformers/paraphrase-MiniLM-L6-v2 \
SCIMLOPSBENCH_SUBSET_SIZE=12 \
bash benchmark_scripts/prepare_assets.sh --offline-ok
```

## GPU overrides

- Single GPU stage forces `CUDA_VISIBLE_DEVICES=0`.
- Multi-GPU stage defaults to `CUDA_VISIBLE_DEVICES=0,1` and uses `python -m torch.distributed.run`.
  - Override number of processes: `SCIMLOPSBENCH_NPROC_PER_NODE=2`
  - Override visible devices: `CUDA_VISIBLE_DEVICES=0,1`

## Running individual stages

```bash
bash benchmark_scripts/run_pyright_missing_imports.sh --repo . --install-pyright
bash benchmark_scripts/prepare_assets.sh --offline-ok
bash benchmark_scripts/run_cpu_entrypoint.sh
python3 benchmark_scripts/check_cuda_available.py
bash benchmark_scripts/run_single_gpu_entrypoint.sh
bash benchmark_scripts/run_multi_gpu_entrypoint.sh
python3 benchmark_scripts/measure_env_size.py
python3 benchmark_scripts/validate_agent_report.py
python3 benchmark_scripts/summarize_results.py
```

