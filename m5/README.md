# M5: Master Table Builder

This module aggregates per-job outputs (produced by m2/run_one_job.py + M4 benchmark_scripts)
into a single master table (CSV + XLSX).

## Expected job directory structure

<results_root>/
  <jobA>/
    job_summary.json
    docker/
      nvidia_smi.txt
      image_id.txt
      inspect.sanitized.json
    agent/
      report.json
      run_metadata.json
      (optional) langfuse_usage.json
    benchmark/
      build_output/
        pyright/results.json
        cpu/results.json
        cuda/results.json
        single_gpu/results.json
        multi_gpu/results.json
        hallucination/results.json
        env_size/results.json
        summary/results.json

## Usage

python -m m5.build_master_table \
  --results-root /path/to/results_root

Outputs:
  /path/to/results_root/_m5/master_table.csv
  /path/to/results_root/_m5/master_table.xlsx
  /path/to/results_root/_m5/m5_build_log.json

If you don't want xlsx:
  python -m m5.build_master_table --results-root ... --no-xlsx
