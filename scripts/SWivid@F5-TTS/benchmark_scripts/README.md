# Benchmark workflow (scripts-only)

This repository includes a fully-scripted benchmark workflow under `benchmark_scripts/` that:
- Runs Pyright missing-import detection
- Prepares minimal dataset + model assets
- Runs minimal CPU inference (repo entrypoint)
- Checks CUDA availability
- Runs minimal single-GPU inference (repo entrypoint)
- Runs minimal multi-GPU distributed training (repo entrypoint)
- Measures environment size (from agent report)
- Validates the agent report and computes hallucination statistics
- Writes a final `build_output/summary/results.json`

## One command

```bash
bash benchmark_scripts/run_all.sh
```

## Agent report

Default report path is `/opt/scimlopsbench/report.json`.

Override:
- CLI: `--report-path <path>`
- Env: `SCIMLOPSBENCH_REPORT=<path>`

## Python interpreter override

You can force a specific python executable for python-based stages:

```bash
bash benchmark_scripts/run_all.sh --python /abs/path/to/python
```

Otherwise stages resolve python from the agent report (`python_path`).

## Outputs

Each stage writes:
- `build_output/<stage>/log.txt`
- `build_output/<stage>/results.json`

Final summary:
- `build_output/summary/log.txt`
- `build_output/summary/results.json`

