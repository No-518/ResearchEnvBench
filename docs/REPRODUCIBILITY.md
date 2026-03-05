# Reproducibility Guide (Paper-Oriented)

This guide is for reproducing the benchmark protocol used in the ResearchEnvBench paper.

## 1. Target Reproduction Scope

Paper-aligned setup:

- Dataset size: `44` repositories (pinned commits).
- Runtime pyramid metrics: `C0` to `C5`.
- Baselines:
  - `codex` (`gpt-5.1-codex`)
  - `claude_code` (`glm-4.7`)
  - `nexau` (`DeepSeek-V3.1-Nex-N1` compatible model ID)

Expected aggregate applicability denominators in this release:

- `C1` denominator (`supports_cpu=yes`): `29`
- `C2` denominator (all repos): `44`
- `C3` denominator (`supports_single_gpu=yes`): `43`
- `C4` denominator (`supports_multi_gpu=yes`): `32`
- `C0` total import denominator: `2858`

## 2. Hardware and Software Prerequisites

Recommended (paper setting):

- Linux x86_64
- Docker with NVIDIA runtime
- NVIDIA driver compatible with CUDA 12.4
- 2 GPUs for meaningful `C4` reproduction (paper used 2x RTX 4090, 24GB each)
- Sufficient disk (200GB+ recommended for repeated full runs)
- Paper driver reference: `550.163.01`

Check host GPU visibility:

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

## 3. Build the Benchmark Image

From repository root:

```bash
docker build -f dockerfiles/ultimate.Dockerfile -t researchenvbench:ultimate .
```

This release Dockerfile uses the official base image `nvidia/cuda:12.4.1-devel-ubuntu22.04`.

## 4. Backend Credential Preparation

All backend credentials/endpoints should be provided via:

```bash
--secrets-env-file /path/to/your_backend.env
```

Quick bootstrap for local files:

```bash
cp secrets/codex.env.example secrets/codex.env
cp secrets/claude_code.env.example secrets/claude_code.env
cp secrets/nexau.env.example secrets/nexau.env
```

### 4.1 Codex (`codex`)

Two supported modes:

- Host session login (default): `m2/run_one_job.py` mounts host Codex auth/config (`~/.codex`, `~/.config/codex`) if present.
- Official OpenAI API mode (recommended): use env file from `secrets/codex.env.example`:

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=your_openai_api_key
CODEX_MODEL=gpt-5.1-codex
```

Backward-compatible aliases are also supported:

```bash
CODEX_BASE_URL=https://your_compatible_endpoint
CODEX_API_KEY=your_codex_api_key
# optional fallback:
# CODEX_TOKEN=your_codex_api_key
```

CLI override is also supported: `--codex-model gpt-5.1-codex`.

### 4.2 Claude Code (`claude_code`)

Recommended env file:

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

CLI override is also supported: `--claude-model glm-4.7`.

Auth file mounting is still supported:

- Setting `CLAUDE_AUTH_DIR` to a host directory to mount, or
- Placing auth material under `secrets/claude_auth` (default lookup path).

### 4.3 NexAU (`nexau` + template variants)

- Start from one of these templates:
  - `secrets/nexau.env.example`
  - `secrets/nex_deepseek31_nexn1.env.example`
  - `secrets/nex_gemini30.env.example`
  - `secrets/nex_claude_sonnet45.env.example`
  - `secrets/nex_minimax25.env.example`

Required core fields:

```bash
LLM_MODEL=your_model_name
LLM_BASE_URL=https://your-openai-or-anthropic-compatible-endpoint
LLM_API_KEY=your_api_key
LLM_API_TYPE=anthropic_chat_completion
LLM_TOOL_CALL_MODE=anthropic
```

Optional fields:

- `SUMMARY_*`
- `EXTRACT_*`
- `LANGFUSE_*`

Config path control:

- `nexau` default: `/opt/nexau/env_setup_config/deepseek-v3.1-nex-n1.yaml`
- `nexau_deepseek31_nexn1` default: `tools/env_setup_runner/nexau_configs/nexau_deepseek31_nexn1.yaml`
- All NexAU backends support env override:
  - `NEXAU_AGENT_CONFIG=/path/to/your_agent_config.yaml`
- If your endpoint is OpenAI-compatible, set:
  - `LLM_API_TYPE=openai_chat_completion`
  - `LLM_TOOL_CALL_MODE=openai`

## 5. Matrices

Already included:

- `m1_repo_manifest_module/manifests/run_matrix.jsonl` (`nexau`)
- `m1_repo_manifest_module/manifests/run_matrix_codex.jsonl` (`codex`)
- `m1_repo_manifest_module/manifests/run_matrix_claude_code.jsonl` (`claude_code`)
- `m1_repo_manifest_module/manifests/run_matrix_smoke_claude_code.jsonl` (smoke)
- `m1_repo_manifest_module/manifests/run_matrix_nexau_deepseek31_nexn1.jsonl` (`nexau_deepseek31_nexn1`)
- `m1_repo_manifest_module/manifests/run_matrix_smoke_nexau_deepseek31_nexn1.jsonl` (smoke)

Optional: regenerate `claude_code` matrix from the codex matrix:

```bash
python tools/matrix/rewrite_baseline_matrix.py \
  --source m1_repo_manifest_module/manifests/run_matrix_codex.jsonl \
  --baseline claude_code \
  --out m1_repo_manifest_module/manifests/run_matrix_claude_code.jsonl
```

### 5.1 Add your own NexAU model baseline

Method A (no code changes, recommended):

- Reuse an existing `nexau_*` matrix and only change env values (`LLM_MODEL/LLM_BASE_URL/LLM_API_KEY/...`).

Method B (new baseline name for separate reporting):

1. Add backend key (for example `nexau_my_model`) in `tools/env_setup_runner/runners.json` by copying `nexau_gemini30`.
2. Choose config path strategy:
   - Use generic env-driven YAML: `tools/env_setup_runner/nexau_configs/nexau_generic_llm.yaml`
   - Or DeepSeek template YAML: `tools/env_setup_runner/nexau_configs/nexau_deepseek31_nexn1.yaml`
   - Or set env override directly: `NEXAU_AGENT_CONFIG=/path/to/your_agent_config.yaml`
3. Generate matrix:

```bash
python tools/matrix/rewrite_baseline_matrix.py \
  --source m1_repo_manifest_module/manifests/run_matrix.jsonl \
  --baseline nexau_my_model \
  --out m1_repo_manifest_module/manifests/run_matrix_nexau_my_model.jsonl
```

4. Run with `--secrets-env-file /path/to/nexau_my_model.env`.

Tip: use `nexau_` prefix for custom names so M2 automatically applies NexAU report-mode defaults.

## 6. Recommended Run Sequence

### 6.1 Smoke test (no credentials required)

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix_codex.jsonl \
  --image researchenvbench:ultimate \
  --limit 1 \
  --skip-agent \
  --build-master-table \
  --run-id smoke_skip_agent
```

### 6.2 Full codex run

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

### 6.3 Full claude_code run

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

### 6.4 Full nexau run

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix.jsonl \
  --image researchenvbench:ultimate \
  --secrets-env-file secrets/nexau.env \
  --network host \
  --build-master-table \
  --run-id paper_nexau
```

### 6.5 Full NexAU DeepSeek3.1 run

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

If an authenticated run is interrupted, resume safely:

```bash
python host_orchestrator.py ... --resume --resume-success-only
```

## 7. Output Artifacts You Should Keep

For each run (`results/<run_id>/`):

- `run_metadata.json`
- `run_summary.json`
- `jobs/<job_id>/job_summary.json`
- `jobs/<job_id>/agent/report.json`
- `jobs/<job_id>/benchmark/build_output/*/results.json`
- `master_table.csv`
- `master_summary.csv`

These are the core auditable artifacts for paper-level claims.

## 8. Mapping to Paper Metrics

`master_summary.csv` corresponds to reported metrics:

- `c0_missing_over_total` -> `C0`
- `c1_cpu_success_over_all` -> `C1`
- `c2_cuda_success_over_all` -> `C2`
- `c3_single_gpu_success_over_all` -> `C3`
- `c4_multi_gpu_success_over_all` -> `C4`
- `c5_*_sum` -> `C5` hallucination counts
- `agent_wall_time_avg_sec`, `env_size_avg_gb`, token fields -> efficiency metrics

## 9. Common Reproducibility Pitfalls

- GPU container runtime not configured:
  - Symptom: `nvidia-smi` works on host but not in Docker.
- Missing backend auth:
  - Symptom: agent timeout or missing `report.json`.
- Running with only one GPU:
  - `C4` is still computed on applicable repos but success will usually drop.
- Missing `openpyxl` on host:
  - `master_table.xlsx` generation may fail (CSV is still produced).

## 10. Pre-Release Checklist (for open-sourcing)

Before publishing artifacts:

- Remove private secrets from all logs and env files.
- Publish only this release workspace; do not package dev workspaces containing real `.env` files.
- Keep `scripts_repos_test_categories.csv` and `m5/c0_repo_baseline_totals.json` in release.
- Provide exact image tag used in experiments.
- Publish run IDs and corresponding `master_summary.csv`.
- Publish command lines used to launch each baseline.
