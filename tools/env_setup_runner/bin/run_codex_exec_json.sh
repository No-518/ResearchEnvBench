#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   run_codex_exec_json.sh <repo_root> <prompt_file> <report_path> [model] [schema_file]
#
# Requires:
#   - OpenAI Codex CLI (`codex`) installed and authenticated
#
# Optional environment variables:
#   CODEX_ARGS: extra args appended to `codex exec` (e.g., "--full-auto --quiet")

repo_root="${1:?repo_root required}"
prompt_file="${2:?prompt_file required}"
report_path="${3:?report_path required}"
model="${4:-}"
schema_file="${5:-}"

if ! command -v codex >/dev/null 2>&1; then
  echo "ERROR: codex CLI not found in PATH" >&2
  exit 127
fi

prompt="$(cat "$prompt_file")"

args=(exec)
if [[ -n "$model" ]]; then
  args+=(--model "$model")
fi

# Ask Codex to write its final message (structured JSON) to report_path.
# If schema_file is provided, it will try to match that JSON schema.
args+=("$prompt" -o "$report_path")
if [[ -n "$schema_file" ]]; then
  args+=(--output-schema "$schema_file")
fi

# Extra user-provided flags.
if [[ -n "${CODEX_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  extra=( ${CODEX_ARGS} )
  args+=("${extra[@]}")
fi

cd "$repo_root"
exec codex "${args[@]}"
