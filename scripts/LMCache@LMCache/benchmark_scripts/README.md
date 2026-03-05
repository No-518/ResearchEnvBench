# LMCache Reproducible Benchmark Workflow (Scripts Only)

## One-command run

From the repo root:

```bash
bash benchmark_scripts/run_all.sh
```

This executes, in order:
`pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary`
and writes all artifacts under `build_output/`.

## Key inputs

### Agent report (python selection)

All stages that require Python resolve the interpreter in this priority order:
1. `--python` (when supported by the stage)
2. `SCIMLOPSBENCH_PYTHON`
3. `python_path` from the agent report JSON
4. Fallback `python` from `PATH` (runner-only; recorded as a warning)

Report path resolution:
1. `--report-path` (python stages that support it)
2. `SCIMLOPSBENCH_REPORT`
3. `/opt/scimlopsbench/report.json`

### Assets (dataset + model)

`benchmark_scripts/prepare_assets.sh` downloads into `benchmark_assets/cache/` and copies into:
- `benchmark_assets/dataset/`
- `benchmark_assets/model/`

It also writes `benchmark_assets/manifest.json` which subsequent stages use.

Defaults (override via env):
- Dataset: `SCIMLOPSBENCH_DATASET_URL` (default: CacheBlend `musique_s.json`)
- Model: `SCIMLOPSBENCH_MODEL_ID` (default: `sshleifer/tiny-gpt2`)
- Model revision: `SCIMLOPSBENCH_MODEL_REVISION` (default: `main`)

## Entrypoint runs (repo-native)

CPU / single-GPU / multi-GPU stages run the repository entrypoint:
`python -m lmcache.v1.standalone`

To keep runs minimal and finite, each run is time-limited (default 8s) and uses small KV shapes.

Common knobs:
- `SCIMLOPSBENCH_ENTRYPOINT_RUN_DURATION_SEC` (default: `8`)
- `SCIMLOPSBENCH_KV_SHAPE` (default: `2,2,8,2,8`)
- `SCIMLOPSBENCH_KVCACHE_SHAPE_SPEC` (default: `(2,2,8,2,8):float16:2`)
- `SCIMLOPSBENCH_MULTI_GPU_DEVICES` (default: `0,1`)
- `SCIMLOPSBENCH_MULTI_GPU_NPROC` (default: `2`)

## Outputs

Each stage writes:
- `build_output/<stage>/log.txt`
- `build_output/<stage>/results.json`

Additional stage outputs:
- `build_output/pyright/pyright_output.json`
- `build_output/pyright/analysis.json`
- `build_output/summary/results.json`

