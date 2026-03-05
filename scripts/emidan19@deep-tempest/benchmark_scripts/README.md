# Deep-Tempest Benchmark Scripts

These scripts add a fully connected, end-to-end benchmark workflow (assets → CPU run → CUDA check → single-GPU → multi-GPU → env size → agent report validation → summary) **without modifying any repository files**.

## One-command run

```bash
bash benchmark_scripts/run_all.sh
```

Outputs are written under:

```text
build_output/<stage>/{log.txt,results.json}
```

Stages (execution order): `pyright`, `prepare`, `cpu`, `cuda`, `single_gpu`, `multi_gpu`, `env_size`, `hallucination`, `summary`.

## Assets

- Dataset: prepared from repository `examples/*.png` into `benchmark_assets/dataset/` (tiny paired sets `mini_1` and `mini_2`).
- Model: downloaded into `benchmark_assets/model/pretrained.pth` (override URL via `DEEPEMPEST_MODEL_URL`).

Example:

```bash
DEEPEMPEST_MODEL_URL="https://.../download" bash benchmark_scripts/prepare_assets.sh
```

## Notes

- Training/inference entrypoint used for runs: `end-to-end/main_train_drunet.py` (run via `PYTHONPATH=end-to-end`).
- Multi-GPU uses PyTorch launcher: `python -m torch.distributed.run ... --dist True`.
- Python interpreter for benchmark stages is resolved from `/opt/scimlopsbench/report.json` (or `SCIMLOPSBENCH_REPORT`) unless overridden.

