# TGATE Benchmark Scripts

These scripts add a fully-connected, end-to-end benchmark workflow **without modifying any repository files**.

## One-command run

```bash
bash benchmark_scripts/run_all.sh
```

Outputs are written under `build_output/` and assets under `benchmark_assets/`.

## Agent report

By default, the runner and Python-dependent stages use:

- `/opt/scimlopsbench/report.json`

Override with:

- `SCIMLOPSBENCH_REPORT=/path/to/report.json`
- `--report-path /path/to/report.json` (highest priority for scripts that accept it)

You may also override the interpreter directly with:

- `SCIMLOPSBENCH_PYTHON=/path/to/python`
- `--python /path/to/python` (where supported)

## Model downloads (Hugging Face)

`benchmark_scripts/prepare_assets.sh` tries to download a TGATE-supported diffusers model snapshot anonymously (no token) into `benchmark_assets/cache/`.

If all candidates are gated, set a token after accepting the model license(s):

- `HF_TOKEN=...` (or `HUGGINGFACE_HUB_TOKEN=...`)

To force a specific TGATE model option:

```bash
bash benchmark_scripts/prepare_assets.sh --model pixart_alpha
```

To run in cache-only mode:

```bash
bash benchmark_scripts/prepare_assets.sh --offline
```

## Notes on CPU / multi-GPU

- `main.py` hardcodes `.to("cuda")` and provides no CLI device flag, so the CPU stage is **skipped** as `repo_not_supported`.
- The repo does not expose a native multi-GPU distributed entrypoint, so the multi-GPU stage is **skipped** as `repo_not_supported`.

