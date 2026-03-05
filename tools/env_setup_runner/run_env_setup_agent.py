#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EnvBench: connect merged prompt templates to Codex / Claude Code runners.

This script is meant for the *benchmark harness repo* (not inside the target repos).

It does four things:
1) Merge prompt templates (system + task + optional appendix) into ONE prompt file.
2) Execute an external code-agent runner (Codex CLI, Claude Code CLI, NexAU, etc.).
3) Force a `report.json` at a fixed path:
   - agent writes file (prompt-enforced), OR
   - runner prints JSON to stdout and we write it to report.json
4) Validate `report.json` and write run artifacts (logs + metadata) to out_dir.

Runner config formats supported:
A) Simple mapping (legacy):
   { "codex": {"command": "...", "env": {...}}, ... }
B) Structured runners.json (your current one):
   {
     "schema_version": 1,
     "backends": {
       "codex": {"kind":"bash","cwd":"{repo_root}","argv":[...], "report": {...}},
       ...
     }
   }

No external deps (stdlib only). JSON schema validation is NOT done here; we only check JSON parse + required keys.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List


@dataclass(frozen=True)
class RunnerSpec:
    name: str
    cwd_template: str
    argv_template: List[str]
    env: Dict[str, str]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def maybe_configure_langfuse_otel_env(env: Dict[str, str]) -> None:
    """Inject best-effort OpenTelemetry exporter settings for Langfuse.

    This is intentionally *best-effort* and non-fatal:
    - If LANGFUSE_* env vars are missing, it does nothing.
    - If a backend ignores OTEL env vars, it is still fine.

    Why this exists:
    Our env-setup agent backends (codex / claude / NexAU) are external CLIs.
    To get token/cost analytics into Langfuse Cloud without modifying those CLIs,
    the most portable option is configuring an OTLP exporter when the backend supports it.

    Langfuse exposes an OTLP HTTP endpoint under:
      <LANGFUSE_HOST>/api/public/otel
    and uses Basic Auth (public_key:secret_key) for authentication.
    """

    host = (env.get("LANGFUSE_HOST") or env.get("LANGFUSE_BASE_URL") or "").strip()
    pk = (env.get("LANGFUSE_PUBLIC_KEY") or "").strip()
    sk = (env.get("LANGFUSE_SECRET_KEY") or "").strip()

    if not (host and pk and sk):
        return

    host = host.rstrip("/")
    otel_base = host + "/api/public/otel"

    # Basic Auth header for OTLP HTTP exporters
    userpass = f"{pk}:{sk}".encode("utf-8")
    b64 = base64.b64encode(userpass).decode("ascii")
    auth_kv = f"Authorization=Basic {b64}"

    # Do not overwrite if already configured by the user.
    env.setdefault("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    env.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", otel_base)
    env.setdefault("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", otel_base + "/v1/traces")
    env.setdefault("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", otel_base + "/v1/logs")
    env.setdefault("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", otel_base + "/v1/metrics")

    # OTLP headers format: comma-separated key=value pairs.
    # If caller already set OTEL_EXPORTER_OTLP_HEADERS, we append Authorization unless present.
    existing = env.get("OTEL_EXPORTER_OTLP_HEADERS", "").strip()
    if existing:
        if "authorization=" not in existing.lower():
            env["OTEL_EXPORTER_OTLP_HEADERS"] = existing + "," + auth_kv
    else:
        env["OTEL_EXPORTER_OTLP_HEADERS"] = auth_kv

    # Provide useful resource attributes for downstream filtering.
    rid = (env.get("RUN_ID") or "").strip()
    jid = (env.get("JOB_ID") or "").strip()
    backend = (env.get("BASELINE") or "").strip()

    extra_attrs = []
    if rid:
        extra_attrs.append(f"scimlopsbench.run_id={rid}")
    if jid:
        extra_attrs.append(f"scimlopsbench.job_id={jid}")
    if backend:
        extra_attrs.append(f"scimlopsbench.baseline={backend}")

    if extra_attrs:
        ra = env.get("OTEL_RESOURCE_ATTRIBUTES", "").strip()
        merged = (ra + ("," if ra else "") + ",".join(extra_attrs))
        env["OTEL_RESOURCE_ATTRIBUTES"] = merged

    # Many tools use OTEL_SERVICE_NAME; set only if user hasn't.
    env.setdefault("OTEL_SERVICE_NAME", "scimlopsbench-envsetup")

    # Claude Code specific: enable telemetry if supported (ignored otherwise).
    env.setdefault("CLAUDE_CODE_ENABLE_TELEMETRY", "1")
    env.setdefault("CLAUDE_CODE_TELEMETRY", "1")


def maybe_write_codex_otel_config(env: Dict[str, str]) -> None:
    """Write a minimal Codex CLI config to enable OTLP export to Langfuse.

    Codex supports OpenTelemetry export via ~/.codex/config.toml.
    We keep this best-effort and non-fatal.
    """

    host = env.get("LANGFUSE_HOST") or env.get("LANGFUSE_BASE_URL") or ""
    pk = env.get("LANGFUSE_PUBLIC_KEY") or ""
    sk = env.get("LANGFUSE_SECRET_KEY") or ""
    if not (host and pk and sk):
        return

    try:
        host = host.rstrip("/")
        otel_base = f"{host}/api/public/otel"
        endpoint = env.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT") or f"{otel_base}/v1/logs"
        auth_b64 = base64.b64encode(f"{pk}:{sk}".encode("utf-8")).decode("ascii")

        cfg = f"""[otel]
enabled = true
environment = "{env.get('RUN_ID', 'envbenchmark')}"
service_name = "scimlopsbench-codex"
service_version = "{env.get('SCIMLOPSBENCH_VERSION', 'unknown')}"
debug = false

[otel.otlp_http]
endpoint = "{endpoint}"

[otel.otlp_http.headers]
Authorization = "Basic {auth_b64}"
"""

        codex_dir = Path.home() / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "config.toml").write_text(cfg, encoding="utf-8")
    except Exception:
        # Never fail the agent run due to telemetry config.
        return
def merge_prompts(
    system_prompt: str,
    task_prompt: str,
    appendix_prompt: Optional[str],
    report_path: str,
) -> str:
    """Merge prompts into a single prompt and inject hard requirements."""

    report_contract_section = f"""

# Mandatory output: report.json

You MUST create a valid JSON report file at:

- {report_path}

Hard requirements:
- The file MUST exist before you finish.
- It MUST be valid JSON (UTF-8).
- It MUST contain the following keys (values may be null if genuinely unverified; explain in notes):
  - "python_path" (non-empty string; absolute path to the final interpreter)
  - "python_version" (string or null; queried from python_path)
  - "torch_version" (string or null; queried from python_path)
  - "cuda_available" (boolean or null; probed via python_path)
  - "gpu_count" (integer or null; probed via python_path)
  - "ddp_expected_ok" (boolean or null; best-faith expectation)
  - "env_tool" (conda|uv|pip|poetry|venv|none)
  - "env_name" (string or null)
  - "notes" (string or null)

Notes:
- Do NOT fabricate results. If you did not verify something, use null and explain in notes.
- All version/capability fields MUST come from running the interpreter you configured.

Example (minimal):

  {{
    "python_path": "/opt/conda/envs/scibench/bin/python",
    "python_version": "3.10.14",
    "torch_version": "2.2.2+cu121",
    "cuda_available": true,
    "gpu_count": 2,
    "ddp_expected_ok": true,
    "env_tool": "conda",
    "env_name": "scibench",
    "notes": "Installed via conda env scibench; verified imports + torch/cuda."
  }}

""".strip("\n")


    blocks = [
        "# System prompt\n" + system_prompt.strip(),
        "# Task prompt\n" + task_prompt.strip(),
    ]
    if appendix_prompt and appendix_prompt.strip():
        blocks.append("# Repo-specific appendix\n" + appendix_prompt.strip())
    blocks.append(report_contract_section)

    return "\n\n---\n\n".join(blocks).strip() + "\n"


def ensure_report_contract(merged_prompt: str, report_path: str) -> str:
    # If the merged prompt already mentions report.json and the required path, keep it.
    if report_path in merged_prompt and "report.json" in merged_prompt.lower():
        return merged_prompt
    return (
        merged_prompt.rstrip()
        + "\n\n---\n\n"
        + (
            f"YOU MUST write a valid JSON file to {report_path} and include keys: "
            "python_path, python_version, torch_version, cuda_available, gpu_count, ddp_expected_ok, env_tool, env_name, notes. "
            "python_path must be a non-empty absolute path to the final interpreter, and the version/capability fields must be derived by running that interpreter (not guessed).\n"
        )
    )



def _extract_json_blob(output: str) -> Tuple[Optional[Any], Optional[str]]:
    """Try to parse a JSON value from the runner stdout.

    Strategy:
    1) whole output (stripped)
    2) last {...} block
    3) last [...] block
    """
    s = output.strip()
    candidates = []
    if s:
        candidates.append(s)

    last_lbrace = s.rfind("{")
    last_rbrace = s.rfind("}")
    if last_lbrace != -1 and last_rbrace != -1 and last_rbrace > last_lbrace:
        candidates.append(s[last_lbrace : last_rbrace + 1])

    last_lbrack = s.rfind("[")
    last_rbrack = s.rfind("]")
    if last_lbrack != -1 and last_rbrack != -1 and last_rbrack > last_lbrack:
        candidates.append(s[last_lbrack : last_rbrack + 1])

    for cand in candidates:
        try:
            return json.loads(cand), cand
        except Exception:
            continue
    return None, None


def _format_str(s: str, variables: Dict[str, str]) -> str:
    try:
        return s.format_map(variables)
    except KeyError as e:
        missing = e.args[0]
        raise KeyError(
            f"Runner template missing variable '{missing}'. Available: {sorted(variables.keys())}"
        )


def load_runner_spec(
    backend: str,
    runner_cmd: Optional[str],
    runners_json: Optional[Path],
) -> RunnerSpec:
    """Load runner spec.

    Priority:
    1) --runner-cmd (string)
    2) --runners-json / --runner-config
    """
    if runner_cmd:
        # user gives full command string
        argv = shlex.split(runner_cmd)
        return RunnerSpec(name=backend, cwd_template="{repo_root}", argv_template=argv, env={})

    if not runners_json:
        raise ValueError("You must provide either --runner-cmd or --runners-json/--runner-config")

    data = json.loads(_read_text(runners_json))

    # Format B: structured runners.json
    if isinstance(data, dict) and "backends" in data and isinstance(data["backends"], dict):
        backends = data["backends"]
        if backend not in backends:
            raise KeyError(f"Backend '{backend}' not found in {runners_json}. Available: {sorted(backends.keys())}")
        entry = backends[backend]
        cwd_t = entry.get("cwd") or "{repo_root}"
        argv_t = entry.get("argv")
        if not isinstance(argv_t, list) or not all(isinstance(x, str) for x in argv_t):
            raise ValueError(f"Backend '{backend}' entry must contain list[str] field 'argv'.")
        env = entry.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError(f"Backend '{backend}' field 'env' must be a dict if provided.")
        env = {str(k): str(v) for k, v in env.items()}
        return RunnerSpec(name=backend, cwd_template=str(cwd_t), argv_template=[str(x) for x in argv_t], env=env)

    # Format A: simple mapping backend -> {command, env}
    if backend not in data:
        raise KeyError(f"Backend '{backend}' not found in {runners_json}. Available: {sorted(data.keys())}")
    entry = data[backend]
    cmd = entry.get("command")
    if not cmd or not isinstance(cmd, str):
        raise ValueError(f"Config entry for '{backend}' must contain a string field 'command'.")
    env = entry.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError(f"Config entry for '{backend}' field 'env' must be a dict.")
    env = {str(k): str(v) for k, v in env.items()}
    argv = shlex.split(cmd)
    return RunnerSpec(name=backend, cwd_template="{repo_root}", argv_template=argv, env=env)


def run_agent(
    argv: list[str],
    cwd: Path,
    env: Dict[str, str],
    log_path: Path,
    timeout_s: Optional[int],
) -> Dict[str, Any]:
    start = time.time()
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_s,
    )
    end = time.time()
    log_path.write_text(proc.stdout or "", encoding="utf-8")
    return {
        "exit_code": proc.returncode,
        "duration_sec": round(end - start, 3),
        "stdout": proc.stdout or "",
    }


def validate_report_file(report_path: Path, required_keys: Optional[list[str]] = None) -> Tuple[bool, str]:
    if not report_path.exists():
        return False, f"missing report file: {report_path}"
    try:
        obj = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"report is not valid JSON: {e}"

    if not isinstance(obj, dict):
        return False, "report JSON must be an object/dict"

    if required_keys:
        missing = [k for k in required_keys if k not in obj]
        if missing:
            return False, f"report JSON missing required keys: {missing}"

    # Enforce: python_path must be a non-empty string if required.
    if required_keys and "python_path" in required_keys:
        v = obj.get("python_path")
        if not isinstance(v, str) or not v.strip():
            return False, "python_path must be a non-empty string"

    return True, "ok"


def _is_nonempty_str(x: Any) -> bool:
    return isinstance(x, str) and bool(x.strip())



def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a code agent with merged prompts and force report.json.")
    ap.add_argument("--backend", required=True, help="Backend name (e.g., codex, claude_code, nexau)")
    ap.add_argument("--repo-root", required=True, type=Path, help="Path to the target repository workspace")

    ap.add_argument("--system-prompt", required=True, type=Path, help="system_prompt.md")
    ap.add_argument("--task-prompt", required=True, type=Path, help="task_prompt.md")

    # 兼容：--appendix / --appendix-prompt
    ap.add_argument("--appendix-prompt", dest="appendix_prompt", type=Path, default=None, help="appendix prompt (optional)")
    ap.add_argument("--appendix", dest="appendix_prompt", type=Path, default=None, help="(alias) appendix prompt (optional)")

    ap.add_argument("--out-dir", required=True, type=Path, help="Directory to write logs/metadata (will be created)")

    ap.add_argument(
        "--report-path",
        type=Path,
        default=Path("/opt/scimlopsbench/report.json"),
        help="Where the report.json must exist",
    )

    ap.add_argument(
        "--report-schema-path",
        type=Path,
        default=Path(__file__).resolve().parent / "report_schema.json",
        help="JSON schema path for runner substitution (codex/claude_code).",
    )

    ap.add_argument(
        "--stdout-json-report",
        choices=["never", "if_missing", "always"],
        default="never",
        help="Whether to force report.json from runner stdout JSON.",
    )

    # Default: enforce the report fields used by downstream benchmark stages (no entrypoint commands).
    ap.add_argument(
        "--required-report-keys",
        default="python_path,python_version,torch_version,cuda_available,gpu_count,ddp_expected_ok,env_tool,env_name,notes",
        help="Comma-separated required keys to validate in report.json",
    )

    ap.add_argument("--runner-cmd", default=None, help="Runner command string template (optional)")

    # 兼容：--runner-config / --runners-json
    ap.add_argument("--runner-config", dest="runners_json", type=Path, default=None, help="Runner config JSON")
    ap.add_argument("--runners-json", dest="runners_json", type=Path, default=None, help="(alias) Runner config JSON")

    ap.add_argument("--timeout-s", type=int, default=None, help="Timeout seconds for the runner process")
    ap.add_argument("--report-retry", type=int, default=1, help="Retry times if report missing/invalid")

    # 可选：让你能覆盖这些 bin 名称（一般不需要）
    ap.add_argument("--python-bin", default=None)
    ap.add_argument("--codex-bin", default=None)
    ap.add_argument("--claude-bin", default=None)

    args = ap.parse_args()

    repo_root: Path = args.repo_root.resolve()
    out_dir: Path = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    report_path: Path = args.report_path
    report_schema_path: Path = args.report_schema_path.resolve()

    required_keys = [k.strip() for k in args.required_report_keys.split(",") if k.strip()]
    required_keys_str = ", ".join(required_keys) if required_keys else ""

    spec = load_runner_spec(args.backend, args.runner_cmd, args.runners_json)

    # Merge prompt
    sys_prompt = _read_text(args.system_prompt)
    task_prompt = _read_text(args.task_prompt)
    appendix = _read_text(args.appendix_prompt) if args.appendix_prompt else None

    merged = merge_prompts(sys_prompt, task_prompt, appendix, str(report_path))
    merged = ensure_report_contract(merged, str(report_path))

    prompt_path = out_dir / "merged_prompt.md"
    prompt_path.write_text(merged, encoding="utf-8")

    # Variables for substitution (match your runners.json placeholders)
    python_bin = args.python_bin or os.environ.get("PYTHON_BIN", "python3")
    codex_bin = args.codex_bin or os.environ.get("CODEX_BIN", "codex")
    claude_bin = args.claude_bin or os.environ.get("CLAUDE_BIN", "claude")

    variables = {
        "repo_root": str(repo_root),
        "prompt_file": str(prompt_path),
        "backend": spec.name,
        "out_dir": str(out_dir),
        "report_path": str(report_path),
        "report_schema_path": str(report_schema_path),
        "runner_dir": str(Path(__file__).resolve().parent),
        "python_bin": python_bin,
        "codex_bin": codex_bin,
        "claude_bin": claude_bin,
        "codex_model": os.environ.get("CODEX_MODEL", ""),
        "claude_model": os.environ.get("CLAUDE_MODEL", ""),
        # NexAU placeholders (match your runners.json defaults; can be overridden by env if needed)
        "nexau_home": os.environ.get("NEXAU_HOME", "/opt/nexau"),
        "nexau_python": os.environ.get("NEXAU_PYTHON", "/opt/nexau/.venv/bin/python"),
        "nexau_dotenv": os.environ.get("NEXAU_DOTENV", "/opt/nexau/.env"),
        "nexau_agent_config": os.environ.get("NEXAU_AGENT_CONFIG", "/opt/nexau/env_setup_config/deepseek-v3.1-nex-n1.yaml"),
        "nexau_deepseek31_agent_config": os.environ.get(
            "NEXAU_DEEPSEEK31_AGENT_CONFIG",
            "/opt/scimlopsbench/harness/tools/env_setup_runner/nexau_configs/nexau_deepseek31_nexn1.yaml",
        ),
        "nexau_generic_agent_config": os.environ.get(
            "NEXAU_GENERIC_AGENT_CONFIG",
            "/opt/scimlopsbench/harness/tools/env_setup_runner/nexau_configs/nexau_generic_llm.yaml",
        ),
        "nexau_run_once_py": os.environ.get("NEXAU_RUN_ONCE_PY", "/opt/nexau/env_setup_config/run_once.py"),
    }

    cwd_str = _format_str(spec.cwd_template, variables)
    argv = [_format_str(a, variables) for a in spec.argv_template]

    base_env = os.environ.copy()
    base_env.update(spec.env)
    base_env.update(
        {
            "ENVBENCH_BACKEND": spec.name,
            "ENVBENCH_REPO_ROOT": str(repo_root),
            "ENVBENCH_PROMPT_FILE": str(prompt_path),
            "ENVBENCH_REPORT_PATH": str(report_path),
        }
    )

    # Best-effort: configure OTLP export to Langfuse if LANGFUSE_* is set
    maybe_configure_langfuse_otel_env(base_env)

    # Codex CLI needs an explicit config file to enable OTLP export
    if spec.name == 'codex':
        maybe_write_codex_otel_config(base_env)

    run_metadata: Dict[str, Any] = {
        "backend": spec.name,
        "repo_root": str(repo_root),
        "runner_argv": argv,
        "runner_cwd": cwd_str,
        "report_path": str(report_path),
        "report_schema_path": str(report_schema_path),
        "prompt_sha256": _sha256_text(merged),
        "utc_started_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "runs": [],
    }

    def try_write_report_from_stdout(stdout_text: str) -> Tuple[bool, str]:
        obj, raw = _extract_json_blob(stdout_text)
        if obj is None or raw is None:
            return False, "could not parse JSON from runner stdout"
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return True, "wrote report.json from stdout JSON"
        except Exception as e:
            return False, f"failed writing report.json: {e}"

    def report_ok() -> Tuple[bool, str]:
        return validate_report_file(report_path, required_keys if required_keys else None)

    for attempt in range(1, max(1, args.report_retry) + 1):
        log_path = out_dir / f"agent_attempt_{attempt}.log"

        if attempt > 1:
            nudge = (
                "\n\n---\n\n"
                "# RETRY INSTRUCTION\n"
                "You did not produce a valid report.json previously. "
                "Do NOT do anything else now: ONLY write/overwrite report.json at the required path, "
                f"and make sure it contains required keys: {required_keys_str}.\n"
            )
            prompt_path.write_text(merged + nudge, encoding="utf-8")

        run = run_agent(argv, Path(cwd_str), base_env, log_path, args.timeout_s)
        run_record = {
            "attempt": attempt,
            "exit_code": run["exit_code"],
            "duration_sec": run["duration_sec"],
            "log_path": str(log_path),
        }

        wrote_from_stdout = False
        stdout_force_msg = None

        if args.stdout_json_report in ("always", "if_missing"):
            exists_before = report_path.exists()
            if args.stdout_json_report == "always" or not exists_before:
                ok, msg = try_write_report_from_stdout(run["stdout"])
                stdout_force_msg = msg
                wrote_from_stdout = ok

        run_record["stdout_json_report_mode"] = args.stdout_json_report
        if stdout_force_msg is not None:
            run_record["stdout_json_report"] = stdout_force_msg

        ok, msg = report_ok()
        run_record["report_valid"] = ok
        run_record["report_validation"] = msg
        run_record["report_written_by_stdout"] = wrote_from_stdout

        run_metadata["runs"].append(run_record)
        if ok:
            break

    run_metadata["utc_finished_at"] = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    report_copy_path = out_dir / "report.json"
    if report_path.exists():
        try:
            report_copy_path.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
            run_metadata["report_copied_to"] = str(report_copy_path)
        except Exception as e:
            run_metadata["report_copy_error"] = str(e)

    write_json(out_dir / "run_metadata.json", run_metadata)

    final_ok, _ = report_ok()
    return 0 if final_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
