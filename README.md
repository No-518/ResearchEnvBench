# ResearchEnvBench

This repository is the released benchmark harness used for the paper:

- `ResearchEnvBench: Benchmarking Agents on Environment Synthesis for Research Code Execution` 

It evaluates whether coding agents can bootstrap runnable ML/HPC research environments from raw repositories.

This repository is the official code repository for the paper
[`ResearchEnvBench: Benchmarking Agents on Environment Synthesis for Research Code Execution`](https://arxiv.org/abs/2603.06739).

## Citation

If you use this repository, please cite:

```bibtex
@misc{wang2026researchenvbenchbenchmarkingagentsenvironment,
      title={ResearchEnvBench: Benchmarking Agents on Environment Synthesis for Research Code Execution}, 
      author={Yubang Wang and Chenxi Zhang and Bowen Chen and Zezheng Huai and Zihao Dai and Xinchi Chen and Yuxin Wang and Yining Zheng and Jingjing Gong and Xipeng Qiu},
      year={2026},
      eprint={2603.06739},
      archivePrefix={arXiv},
      primaryClass={cs.SE},
      url={https://arxiv.org/abs/2603.06739}, 
}
```

## What This Repo Contains

- 44 pinned research repositories (via manifest + run matrices).
- A host orchestrator that runs jobs in isolated Docker containers.
- A strict runtime verification pipeline (`C0` to `C5`):
  - `C0`: static missing-import check
  - `C1`: CPU entrypoint run
  - `C2`: CUDA alignment
  - `C3`: single-GPU execution
  - `C4`: multi-GPU/DDP execution (when applicable)
  - `C5`: report hallucination audit
- Multi-backend agent support:
  - `codex`
  - `claude_code`
  - `nexau`
  - `nexau_deepseek31_nexn1`
  - `nexau_gemini30`
  - `nexau_claude_sonnet45`
  - `nexau_minimax25`

Paper baselines and recommended model IDs:

- `codex`: `gpt-5.1-codex`
- `claude_code`: `glm-4.7`
- `nexau`: `nex-agi/deepseek-v3.1-nex-1` (or your equivalent DeepSeek-V3.1-Nex-N1 endpoint/model ID)

## Repository Map

- `host_orchestrator.py`: top-level runner for full benchmark jobs.
- `m2/run_one_job.py`: one-job lifecycle (container start, agent run, benchmark run, summary).
- `scripts/<owner>@<repo>/benchmark_scripts/`: fixed per-repo runtime probes.
- `m1_repo_manifest_module/manifests/`: pinned repo manifests + run matrices.
- `m5/build_master_table.py`: aggregate run outputs into CSV/XLSX summaries.
- `tools/env_setup_runner/`: backend runner + report contract enforcement.
- `scripts_repos_test_categories.csv`: stage applicability (`C1/C3/C4` denominators).

## Quick Start (Pipeline Smoke Test)

This checks orchestration + benchmark wiring without requiring model/API credentials.

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix_codex.jsonl \
  --image researchenvbench:ultimate \
  --limit 1 \
  --skip-agent \
  --build-master-table \
  --run-id smoke_skip_agent
```

Outputs are written to:

- `results/smoke_skip_agent/`

## Paper-Oriented Reproduction

For reproducibility details aligned to the paper (hardware assumptions, backend setup, exact commands, output checks), see:

- `docs/REPRODUCIBILITY.md`

## Credential Setup (URL + API Key)

Pass credentials via `--secrets-env-file <your_env_file>`.

Recommended bootstrap:

```bash
cp secrets/codex.env.example secrets/codex.env
cp secrets/claude_code.env.example secrets/claude_code.env
cp secrets/nexau.env.example secrets/nexau.env
```

### 1) Codex (`codex`)

Two supported ways:

- Session auth (default): pre-login on host, then M2 mounts `~/.codex` / `~/.config/codex`.
- Official OpenAI API mode (recommended): start from `secrets/codex.env.example`.

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=your_openai_api_key
CODEX_MODEL=gpt-5.1-codex
```

Backward-compatible aliases are supported for dev-era env files:

```bash
CODEX_BASE_URL=https://your_compatible_endpoint
CODEX_API_KEY=your_codex_api_key
# optional fallback if CODEX_API_KEY is absent:
# CODEX_TOKEN=your_codex_api_key
```

The runner maps `CODEX_BASE_URL/CODEX_API_KEY/CODEX_TOKEN` to `OPENAI_BASE_URL/OPENAI_API_KEY`.
You can also set model from CLI: `--codex-model gpt-5.1-codex`.

### 2) Claude Code (`claude_code`)

Env file example (start from `secrets/claude_code.env.example`):

```bash
ANTHROPIC_BASE_URL=https://your-anthropic-compatible-endpoint
ANTHROPIC_API_KEY=your_claude_api_key
CLAUDE_MODEL=glm-4.7
# optional alternative:
# ANTHROPIC_AUTH_TOKEN=your_claude_api_key
```

If `ANTHROPIC_BASE_URL` is not set, the runner default is:

```bash
ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic
```

You can also set model from CLI: `--claude-model glm-4.7`.

### 3) NexAU (`nexau` / `nexau_*`)

Use one of these templates and fill values:

- `secrets/nexau.env.example`
- `secrets/nex_deepseek31_nexn1.env.example`
- `secrets/nex_gemini30.env.example`
- `secrets/nex_claude_sonnet45.env.example`
- `secrets/nex_minimax25.env.example`

Core env fields:

```bash
LLM_MODEL=your_model_name
LLM_BASE_URL=https://your-openai-or-anthropic-compatible-endpoint
LLM_API_KEY=your_api_key
LLM_API_TYPE=anthropic_chat_completion
LLM_TOOL_CALL_MODE=anthropic
```

Config-level control (important for reproducibility):

- `nexau` defaults to official NexAU DeepSeek config:
  - `/opt/nexau/env_setup_config/deepseek-v3.1-nex-n1.yaml`
- `nexau_deepseek31_nexn1` defaults to harness DeepSeek config:
  - `tools/env_setup_runner/nexau_configs/nexau_deepseek31_nexn1.yaml`
- Any NexAU backend can override YAML via env file:
  - `NEXAU_AGENT_CONFIG=/path/to/your_config.yaml`
- If your endpoint is OpenAI-compatible, set:
  - `LLM_API_TYPE=openai_chat_completion`
  - `LLM_TOOL_CALL_MODE=openai`

Also supported for tracing/summarization:

- `SUMMARY_*`
- `EXTRACT_*`
- `LANGFUSE_*`

## Minimal Run Commands (Per Backend)

### 1) Codex baseline (`codex`)

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix_codex.jsonl \
  --image researchenvbench:ultimate \
  --secrets-env-file secrets/codex.env \
  --codex-model gpt-5.1-codex \
  --network host \
  --build-master-table \
  --run-id paper_codex
```

### 2) Claude Code baseline (`claude_code`)

Use the included matrix:

```bash
m1_repo_manifest_module/manifests/run_matrix_claude_code.jsonl
```

If you want to regenerate it from the codex matrix (also re-generates `job_id` safely):

```bash
python tools/matrix/rewrite_baseline_matrix.py \
  --source m1_repo_manifest_module/manifests/run_matrix_codex.jsonl \
  --baseline claude_code \
  --out m1_repo_manifest_module/manifests/run_matrix_claude_code.jsonl
```

Then run:

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix_claude_code.jsonl \
  --image researchenvbench:ultimate \
  --secrets-env-file secrets/claude_code.env \
  --claude-model glm-4.7 \
  --network host \
  --build-master-table \
  --run-id paper_claude_code
```

### 3) NexAU baseline (`nexau`)

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix.jsonl \
  --image researchenvbench:ultimate \
  --secrets-env-file secrets/nexau.env \
  --network host \
  --build-master-table \
  --run-id paper_nexau
```

### 3.1) NexAU DeepSeek3.1 example (`nexau_deepseek31_nexn1`)

Use included matrix + env template:

```bash
cp secrets/nex_deepseek31_nexn1.env.example secrets/nex_deepseek31_nexn1.env
```

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix_nexau_deepseek31_nexn1.jsonl \
  --image researchenvbench:ultimate \
  --baseline nexau_deepseek31_nexn1 \
  --secrets-env-file secrets/nex_deepseek31_nexn1.env \
  --network host \
  --build-master-table \
  --run-id paper_nexau_deepseek31_nexn1
```

For smoke validation, use:

- `m1_repo_manifest_module/manifests/run_matrix_smoke_nexau_deepseek31_nexn1.jsonl`

For public/provider-flexible reproduction, you can use template NexAU backends (`nexau_deepseek31_nexn1`, `nexau_gemini30`, `nexau_claude_sonnet45`, `nexau_minimax25`) with env templates in `secrets/*.env.example`.

## Add Your Own NexAU Model

### Method A: no code changes (recommended)

Reuse an existing `nexau_*` backend and only change env file values:

1. Set `LLM_MODEL/LLM_BASE_URL/LLM_API_KEY/LLM_API_TYPE/LLM_TOOL_CALL_MODE`.
2. Run with one existing matrix, for example `run_matrix_nexau_gemini30.jsonl`.
3. Keep `--secrets-env-file` pointing to your filled env file.

### Method B: create a new named baseline (for separate reporting)

1. Add a new backend key (for example `nexau_my_model`) in:
   - `tools/env_setup_runner/runners.json`
   - Copy `nexau_gemini30` backend (generic env-driven config), or `nexau_deepseek31_nexn1` (DeepSeek-oriented config).
2. Optional: set `NEXAU_AGENT_CONFIG` in env file to your own YAML path (official or custom), no code change required.
3. Generate a new matrix with your baseline name:

```bash
python tools/matrix/rewrite_baseline_matrix.py \
  --source m1_repo_manifest_module/manifests/run_matrix.jsonl \
  --baseline nexau_my_model \
  --out m1_repo_manifest_module/manifests/run_matrix_nexau_my_model.jsonl
```

4. Run:

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix_nexau_my_model.jsonl \
  --image researchenvbench:ultimate \
  --secrets-env-file /path/to/nexau_my_model.env \
  --network host \
  --build-master-table \
  --run-id paper_nexau_my_model
```

Tip: use `nexau_` prefix for custom names so M2 automatically uses the NexAU report mode defaults.

## Interpreting Outputs

After each run:

- Job-level outputs: `results/<run_id>/jobs/<job_id>/...`
- Run-level summary: `results/<run_id>/master_summary.csv`
- Full table: `results/<run_id>/master_table.csv`

Important denominator checks (paper setting):

- `C1` denominator: `29`
- `C2` denominator: `44`
- `C3` denominator: `43`
- `C4` denominator: `32`
- `C0` total denominator: `2858`

These are enforced by tests and fixed benchmark metadata.

## Notes

- Default target runtime in the paper is Ubuntu 22.04 + CUDA 12.4 + 2x RTX 4090.
- Paper driver reference is `550.163.01`.
- `dockerfiles/ultimate.Dockerfile` uses the official base image `nvidia/cuda:12.4.1-devel-ubuntu22.04`.
- Benchmarks are designed around no-modification of tracked repo source files during agent setup.
- For backend-specific runner details, see `tools/env_setup_runner/README.md`.
