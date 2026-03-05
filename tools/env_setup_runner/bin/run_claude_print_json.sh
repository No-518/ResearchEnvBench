#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   run_claude_print_json.sh <repo_root> <prompt_file> [model] [schema_file]
#
# Requires:
#   - Claude Code CLI (`claude`) installed and authenticated
#
# This script prints the JSON result to stdout (when supported by the CLI).
# Use the Python runner with `--stdout-json-report always` to write a report file.

repo_root="${1:?repo_root required}"
prompt_file="${2:?prompt_file required}"
model="${3:-}"
schema_file="${4:-}"

# For headless runs (e.g., inside containers), Claude Code may require a minimal
# config to skip onboarding. This script sets up ~/.claude.json and
# ~/.claude/settings.json under $HOME, and defaults to GLM's Anthropic-compatible
# endpoint unless the caller already provided env overrides.
strip_outer_quotes() {
  local v="$1"
  if [[ "${v:0:1}" == "\"" && "${v: -1}" == "\"" ]]; then
    v="${v:1:-1}"
  elif [[ "${v:0:1}" == "'" && "${v: -1}" == "'" ]]; then
    v="${v:1:-1}"
  fi
  printf '%s' "$v"
}

# Normalize secrets from --env-file (docker keeps quotes as literal characters).
if [[ -n "${ANTHROPIC_AUTH_TOKEN-}" ]]; then
  export ANTHROPIC_AUTH_TOKEN
  ANTHROPIC_AUTH_TOKEN="$(strip_outer_quotes "$ANTHROPIC_AUTH_TOKEN")"
fi
if [[ -n "${ANTHROPIC_API_KEY-}" ]]; then
  export ANTHROPIC_API_KEY
  ANTHROPIC_API_KEY="$(strip_outer_quotes "$ANTHROPIC_API_KEY")"
fi
if [[ -z "${ANTHROPIC_API_KEY-}" && -n "${ANTHROPIC_AUTH_TOKEN-}" ]]; then
  export ANTHROPIC_API_KEY="$ANTHROPIC_AUTH_TOKEN"
fi
if [[ -z "${ANTHROPIC_AUTH_TOKEN-}" && -n "${ANTHROPIC_API_KEY-}" ]]; then
  export ANTHROPIC_AUTH_TOKEN="$ANTHROPIC_API_KEY"
fi

# Default to GLM's Anthropic-compatible gateway (overrideable by caller).
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://open.bigmodel.cn/api/anthropic}"
export API_TIMEOUT_MS="${API_TIMEOUT_MS:-3000000}"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-1}"

if [[ -z "${ANTHROPIC_AUTH_TOKEN-}" && -z "${ANTHROPIC_API_KEY-}" ]]; then
  echo "Missing ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY in environment." >&2
  exit 2
fi

# Ensure headless onboarding is marked as complete.
printf '%s\n' '{"hasCompletedOnboarding": true}' > "${HOME}/.claude.json"

# Write ~/.claude/settings.json to mirror env settings (best-effort).
python3 - <<'PY' >/dev/null 2>&1 || true
import json
import os
from pathlib import Path

home = Path(os.environ.get("HOME", "/tmp")).expanduser()
settings_path = home / ".claude" / "settings.json"
settings_path.parent.mkdir(parents=True, exist_ok=True)

env = {}
for k in (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "API_TIMEOUT_MS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
):
    v = os.environ.get(k)
    if v is not None:
        env[k] = v

# Keep numeric shape for CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC if possible.
try:
    if "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in env:
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = int(str(env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"]).strip())
except Exception:
    pass

settings = {"env": env}
settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

# Read the merged prompt (can include newlines)
prompt_text="$(cat "$prompt_file")"

cd "$repo_root"

claude_bin="${CLAUDE_BIN:-claude}"

args=(
  "--allow-dangerously-skip-permissions"
  "--permission-mode" "bypassPermissions"
  "--add-dir" "$repo_root"
  "--add-dir" "/data/results"
  "--add-dir" "/opt/scimlopsbench"
  "--add-dir" "${HOME}"
  "--add-dir" "${XDG_CONFIG_HOME:-$HOME/.config}"
  "--add-dir" "/tmp"
  "--output-format" "text"
  "-p" "$prompt_text"
)

if [[ -n "$model" ]]; then
  args=("--model" "$model" "${args[@]}")
fi

# When provided, this constrains the JSON shape.
if [[ -n "$schema_file" && -f "$schema_file" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required to load --json-schema from a file: $schema_file" >&2
    exit 2
  fi
  schema_json="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], "r", encoding="utf-8"))))' "$schema_file")"
  args+=("--json-schema" "$schema_json")
fi

# The caller can append extra Claude CLI args.
if [[ -n "${CLAUDE_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  extra=( $CLAUDE_ARGS )
  args+=("${extra[@]}")
fi

exec "$claude_bin" "${args[@]}"
