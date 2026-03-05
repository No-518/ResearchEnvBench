# EnvBench Agent Runner (Codex / Claude Code)

This package gives you a **single Python entrypoint** plus tiny shell wrappers so you can:

1. merge your prompt templates into **one** merged prompt
2. run an external **code-agent runner** (Codex CLI, Claude Code CLI, etc.)
3. **force** a machine-readable `report.json`
4. validate that report and log everything to a per-run folder

The key thing that makes this robust is the enforcement mechanism: besides asking the agent to write `report.json`, you can also **capture JSON from the runner's stdout and write the report yourself**.

---

## Files

- `run_env_setup_agent.py` — main Python program
- `runners.example.json` — sample runner config (command templates)
- `report_schema.json` — minimal JSON schema you can tighten later
- `bin/run_codex_exec_json.sh` — wrapper that reads a prompt file and calls `codex exec`
- `bin/run_claude_print_json.sh` — wrapper that reads a prompt file and calls `claude -p`

---

## How it enforces `report.json`

You choose a mode via `--stdout-json-report`:

- `never`: only accept a report file that the agent wrote to `--report-path`.
- `if_missing`: if the report file is missing, try to parse runner stdout as JSON and write the report.
- `always`: always parse runner stdout as JSON and write the report (useful when the CLI prints JSON but does not write files).

---

## Typical usage

### 1) Put your prompt files somewhere

- `system_prompt.md`
- `task_prompt.md`
- per-repo `task_prompt_appendix.md` (optional)

### 2) Create a runner config

Copy `runners.example.json` to `runners.json` and edit the `command` strings.

Command templates can use placeholders:
- `{repo_root}`
- `{prompt_file}` (the merged prompt path created by the Python runner)
- `{report_path}`
- `{runner_dir}` (this package folder)

### 3) Run

```bash
python run_env_setup_agent.py \
  --runner-config runners.json \
  --backend claude_code \
  --repo-root /data/project/repo \
  --system-prompt /path/to/system_prompt.md \
  --task-prompt /path/to/task_prompt.md \
  --appendix /data/project/task_prompt_appendix.md \
  --out-dir /data/project/runlogs/repo1 \
  --report-path /opt/scimlopsbench/report.json \
  --stdout-json-report always
```

Outputs:
- `out-dir/merged_prompt.md`
- `out-dir/agent.log`
- `out-dir/run_metadata.json`
- `out-dir/report_copy.json` (always copied from report path if it exists)

---

## Recommended runner flags (why the wrappers exist)

- **Claude Code** supports `-p` for non-interactive prompts and `--output-format text` + `--json-schema` to constrain stdout into directly-parseable JSON. (See Claude Code CLI reference.)
- **Codex CLI** supports `codex exec` and can write structured output via `--output-schema` and `-o <file>`. (See OpenAI Codex CLI reference.)

The included wrappers implement these patterns so you don't fight with shell quoting when your merged prompt contains lots of newlines.

---

## Tightening the report contract

Start with the provided `report_schema.json` and gradually add required fields once you finalize your benchmark summary tables.

You can also pass `--required-report-keys k1,k2,...` to fail fast if the produced JSON is missing critical keys.

---

## Backends kept in this release

This release keeps existing runner behavior for:

- `codex`
- `claude_code`
- `nexau` (legacy DeepSeek config)

Model selection hooks:

- `CODEX_MODEL` (for `codex`)
- `CLAUDE_MODEL` (for `claude_code`)

And adds NexAU template backends for model-swapping through env files:

- `nexau_deepseek31_nexn1`
- `nexau_gemini30`
- `nexau_claude_sonnet45`
- `nexau_minimax25`

Config defaults:

- `nexau_deepseek31_nexn1`:
  - `tools/env_setup_runner/nexau_configs/nexau_deepseek31_nexn1.yaml`
- `nexau_gemini30` / `nexau_claude_sonnet45` / `nexau_minimax25`:
  - `tools/env_setup_runner/nexau_configs/nexau_generic_llm.yaml`

This generic YAML reads from env:

- `LLM_MODEL`
- `LLM_BASE_URL`
- `LLM_API_KEY`
- `LLM_API_TYPE`
- `LLM_TOOL_CALL_MODE`

---

## Endpoint / API Key Configuration

This harness expects credentials from an env-file passed by host orchestrator (`--secrets-env-file`).

### Codex (`codex`)

Two modes:

- Session mode: rely on host `~/.codex` / `~/.config/codex`.
- Official OpenAI API mode: provide env values:

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=your_openai_api_key
```

Backward-compatible aliases are supported:

```bash
CODEX_BASE_URL=https://your_compatible_endpoint
CODEX_API_KEY=your_codex_api_key
# optional fallback:
# CODEX_TOKEN=your_codex_api_key
```

The codex backend maps `CODEX_BASE_URL/CODEX_API_KEY/CODEX_TOKEN` to `OPENAI_BASE_URL/OPENAI_API_KEY`.
To pin a model without editing code, set:

```bash
CODEX_MODEL=gpt-5.1-codex
```

### Claude Code (`claude_code`)

```bash
ANTHROPIC_BASE_URL=https://your-anthropic-compatible-endpoint
ANTHROPIC_API_KEY=your_claude_api_key
# optional alternative:
# ANTHROPIC_AUTH_TOKEN=your_claude_api_key
```

If `ANTHROPIC_BASE_URL` is unset, the runner default is:

```bash
ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic
```

To pin a model without editing code, set:

```bash
CLAUDE_MODEL=glm-4.7
```

### NexAU (`nexau` / `nexau_*`)

```bash
LLM_MODEL=your_model_name
LLM_BASE_URL=https://your-openai-or-anthropic-compatible-endpoint
LLM_API_KEY=your_api_key
LLM_API_TYPE=anthropic_chat_completion
LLM_TOOL_CALL_MODE=anthropic
```

Release defaults in `runners.json` are also `anthropic_chat_completion` + `anthropic` for all `nexau*` backends.

All NexAU backends allow config override via env:

```bash
NEXAU_AGENT_CONFIG=/path/to/your_agent_config.yaml
```

---

## Secrets templates

Create real files from:

- `secrets/codex.env.example`
- `secrets/claude_code.env.example`
- `secrets/nexau.env.example`
- `secrets/nex_deepseek31_nexn1.env.example`
- `secrets/nex_gemini30.env.example`
- `secrets/nex_claude_sonnet45.env.example`
- `secrets/nex_minimax25.env.example`

`LLM_API_TYPE` examples:

- `openai_chat_completion`
- `anthropic_chat_completion`

`LLM_TOOL_CALL_MODE` examples:

- `openai`
- `anthropic`

## DeepSeek3.1 NexAU Example (Config + Secrets)

1. Create env file:

```bash
cp secrets/nex_deepseek31_nexn1.env.example secrets/nex_deepseek31_nexn1.env
```

2. Choose config strategy:

- Keep default backend config:
  - `nexau_deepseek31_nexn1 -> tools/env_setup_runner/nexau_configs/nexau_deepseek31_nexn1.yaml`
- Or override in env file:
  - `NEXAU_AGENT_CONFIG=/opt/nexau/env_setup_config/deepseek-v3.1-nex-n1.yaml`
  - `NEXAU_AGENT_CONFIG=/opt/scimlopsbench/harness/tools/env_setup_runner/nexau_configs/nexau_deepseek31_nexn1.yaml`
  - If your endpoint is OpenAI-compatible, set:
    - `LLM_API_TYPE=openai_chat_completion`
    - `LLM_TOOL_CALL_MODE=openai`

3. Run (smoke):

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix_smoke_nexau_deepseek31_nexn1.jsonl \
  --image researchenvbench:ultimate \
  --baseline nexau_deepseek31_nexn1 \
  --secrets-env-file secrets/nex_deepseek31_nexn1.env \
  --network host \
  --build-master-table
```

---

## Add a New NexAU Model

### Method A: no code changes (recommended)

Reuse one existing `nexau_*` backend and only change env-file values (`LLM_MODEL/LLM_BASE_URL/LLM_API_KEY/...`).

For config-level changes (tools/middlewares/system prompt), set:

- `NEXAU_AGENT_CONFIG=/path/to/your_custom.yaml`

### Method B: new backend name (for separate reporting)

1. Add a new backend key (for example `nexau_my_model`) in `tools/env_setup_runner/runners.json` by copying:
   - `nexau_gemini30` (generic env-driven YAML), or
   - `nexau_deepseek31_nexn1` (DeepSeek-oriented YAML).
2. Optionally expose a placeholder in `placeholders` for your default YAML path.
3. Create matrix with the new baseline:

```bash
python tools/matrix/rewrite_baseline_matrix.py \
  --source m1_repo_manifest_module/manifests/run_matrix.jsonl \
  --baseline nexau_my_model \
  --out m1_repo_manifest_module/manifests/run_matrix_nexau_my_model.jsonl
```

4. Run host orchestrator with:
   - `--run-matrix ...run_matrix_nexau_my_model.jsonl`
   - `--baseline nexau_my_model`
   - `--secrets-env-file /path/to/nexau_my_model.env`

Tip: use `nexau_` prefix for custom names.

---

## Matrix generation (full + smoke)

Generate model-specific matrices from the base NexAU matrix:

```bash
python tools/matrix/make_nexau_model_matrices.py \
  --source m1_repo_manifest_module/manifests/run_matrix.jsonl \
  --out-dir m1_repo_manifest_module/manifests \
  --smoke-repo Auto1111SDK/Auto1111SDK
```

Outputs:

- full (44 repos):
  - `run_matrix_nexau_deepseek31_nexn1.jsonl`
  - `run_matrix_nexau_gemini30.jsonl`
  - `run_matrix_nexau_claude_sonnet45.jsonl`
  - `run_matrix_nexau_minimax25.jsonl`
- smoke (1 repo):
  - `run_matrix_smoke_nexau_deepseek31_nexn1.jsonl`
  - `run_matrix_smoke_nexau_gemini30.jsonl`
  - `run_matrix_smoke_nexau_claude_sonnet45.jsonl`
  - `run_matrix_smoke_nexau_minimax25.jsonl`

---

## Smoke run commands

From harness root:

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix_smoke_nexau_deepseek31_nexn1.jsonl \
  --image researchenvbench:ultimate \
  --baseline nexau_deepseek31_nexn1 \
  --secrets-env-file secrets/nex_deepseek31_nexn1.env \
  --network host \
  --build-master-table
```

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix_smoke_nexau_gemini30.jsonl \
  --image researchenvbench:ultimate \
  --baseline nexau_gemini30 \
  --secrets-env-file secrets/nex_gemini30.env \
  --network host \
  --build-master-table
```

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix_smoke_nexau_claude_sonnet45.jsonl \
  --image researchenvbench:ultimate \
  --baseline nexau_claude_sonnet45 \
  --secrets-env-file secrets/nex_claude_sonnet45.env \
  --network host \
  --build-master-table
```

```bash
python host_orchestrator.py \
  --run-matrix m1_repo_manifest_module/manifests/run_matrix_smoke_nexau_minimax25.jsonl \
  --image researchenvbench:ultimate \
  --baseline nexau_minimax25 \
  --secrets-env-file secrets/nex_minimax25.env \
  --network host \
  --build-master-table
```
