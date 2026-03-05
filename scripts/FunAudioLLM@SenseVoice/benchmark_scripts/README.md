# SenseVoice Reproducible Benchmark Workflow (Scripts-Only)

## One-command run

From the repository root:

```bash
bash benchmark_scripts/run_all.sh
```

This executes (in order): `pyright -> prepare -> cpu -> cuda -> single_gpu -> multi_gpu -> env_size -> hallucination -> summary`.

## Output locations

Each stage writes:

- `build_output/<stage>/log.txt`
- `build_output/<stage>/results.json`

Additional stage artifacts may be written under the same stage directory (e.g. HTTP responses).

## Asset cache / offline reuse

Assets are written only under:

- `benchmark_assets/cache/`
- `benchmark_assets/dataset/`
- `benchmark_assets/model/`

The cache is designed for offline reuse on subsequent runs.

## Report / Python selection

Most stages require a valid agent report at the default location:

`/opt/scimlopsbench/report.json`

Override the report path with:

- env var: `SCIMLOPSBENCH_REPORT=/path/to/report.json`

Override the python interpreter for all `runner.py`-based stages with:

- env var: `SCIMLOPSBENCH_PYTHON=/path/to/python`

## Prepare stage configuration

Override dataset/model selection (defaults are based on the repository README examples):

- `SENSEVOICE_BENCH_DATASET_URL` (default: `https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/test_audio/asr_example_en.wav`)
- `SENSEVOICE_BENCH_MODEL_ID` (default: `iic/SenseVoiceSmall`)

