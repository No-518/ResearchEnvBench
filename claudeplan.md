# Claude Code Headless Enablement Plan

Goals
- Install Claude Code CLI in the ultimate image.
- Reuse existing logged-in session inside the container.
- Ensure outbound traffic uses the current proxy/tun setup.
- Produce clean JSON for `report.json` without stderr noise.

Inputs Needed
- Host OS and how `claude` is installed (npm, installer, apt, pipx, other).
- Exact host path(s) for Claude login state (examples: `~/.config/claude`, `~/.config/anthropic`, `~/.claude`, `~/.local/share/claude`).
- Proxy details: is it system-level TUN only, or a local HTTP/SOCKS proxy with a port?
- Whether we should keep `--network host` (current M2 default).

Findings (host scan)
- CLI binary: `~/.local/bin/claude` -> `~/.local/share/claude/versions/2.1.12`
- Login config file: `~/.claude.json` (likely OAuth token storage)
- Proxy ports (from start_mihomo.sh notes):
  - Mixed (HTTP+SOCKS): 127.0.0.1:7897
  - SOCKS: 127.0.0.1:7898
  - HTTP: 127.0.0.1:7899

Planned Steps
1) Inventory host CLI version and login state paths; confirm what to mount.
2) Add Claude CLI install to `dockerfiles/ultimate.Dockerfile` (non-interactive; avoid hanging installer).
3) Update `m2/run_one_job.py` to copy `~/.claude.json` into `/opt/claude_config/.claude.json` (no host mount) and set `CLAUDE_CONFIG_DIR`.
4) Update `tools/env_setup_runner/runners.json` to keep stdout as pure JSON and log stderr separately; pass proxy env if needed.
5) Rebuild the image and run a minimal `claude -p --output-format json` check inside the container.
6) Run a small `run_env_setup_agent.py` job and verify `report.json` validity.

Status Update
- `~/.claude.json` has no token; OAuth token is in `.env` as `CLAUDE_CODE_SESSION_ACCESS_TOKEN`/`ANTHROPIC_AUTH_TOKEN`.
- `runners.json` updated to separate stderr and use bypass mode + scoped `--add-dir` (requires non-root).
- Plan: run Claude under a non-root user so `--allow-dangerously-skip-permissions` is permitted; scope dirs to repo/results.

Proxy Notes
- If the host TUN is system-level, containers using `--network host` should follow host routing without extra env.
- If the CLI requires explicit proxy env, set `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY`.
- For `--network host`, `HTTP_PROXY=http://127.0.0.1:PORT` should work if a local proxy exists.
- For bridge networking, use `host.docker.internal` (or host-gateway) and set the proxy env to that address.
