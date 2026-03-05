# Reproducible Benchmark Workflow

Run the full benchmark chain from the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

## Outputs

Stage outputs are written under `build_output/<stage>/`:

- `log.txt`
- `results.json`

Assets are prepared under `benchmark_assets/`:

- `benchmark_assets/cache/` (download/cache root)
- `benchmark_assets/dataset/`
- `benchmark_assets/model/`

## Key Configuration

- `SCIMLOPSBENCH_REPORT`: override agent report path (default `/opt/scimlopsbench/report.json`)
- `SCIMLOPSBENCH_MODEL_ID`: HF model id used by `examples/llama.py` (default `hf-internal-testing/tiny-random-LlamaForCausalLM`)
- `SCIMLOPSBENCH_MODEL_REVISION`: HF revision (default `main`)
- `SCIMLOPSBENCH_OFFLINE=1`: do not attempt network downloads (reuses existing cache if present)
- `SCIMLOPSBENCH_MULTI_GPU_VISIBLE_DEVICES`: override multi-GPU device list (default `0,1`)

