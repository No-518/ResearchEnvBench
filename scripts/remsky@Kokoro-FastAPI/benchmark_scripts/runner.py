#!/usr/bin/env python3
"""
Unified executor and shared utilities for the benchmark workflow.

This file is intentionally self-contained (stdlib only) so it can run in minimal environments.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_timestamp() -> str:
    # ISO-8601, seconds precision
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def safe_read_text(path: Path, max_bytes: int = 4_000_000) -> str:
    try:
        data = path.read_bytes()
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def tail_lines(text: str, min_lines: int = 150, max_lines: int = 250) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    n = min(max_lines, max(min_lines, min(len(lines), max_lines)))
    return "\n".join(lines[-n:])


def get_git_commit(root: Path) -> str:
    git_dir = root / ".git"
    if not git_dir.exists():
        return ""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT
        )
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def load_json_file(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "json_root_not_object"
        return data, None
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "unknown"


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


@dataclass(frozen=True)
class PythonResolution:
    cmd: List[str]
    source: str  # cli|env|report|path_fallback
    warning: str = ""
    report_path: str = ""


def _parse_python_cmd(value: str) -> List[str]:
    # Accept either a plain path or a shell-style command string.
    value = value.strip()
    if not value:
        return []
    return shlex.split(value)


def resolve_python_cmd(
    *,
    cli_python: Optional[str],
    report_path: Path,
    allow_fallback: bool,
) -> Tuple[Optional[PythonResolution], Optional[str]]:
    if cli_python:
        cmd = _parse_python_cmd(cli_python)
        if not cmd:
            return None, "empty_cli_python"
        return PythonResolution(cmd=cmd, source="cli", report_path=str(report_path)), None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        cmd = _parse_python_cmd(env_python)
        if not cmd:
            return None, "empty_env_python"
        return PythonResolution(cmd=cmd, source="env", report_path=str(report_path)), None

    report, err = load_json_file(report_path)
    if report is None:
        if allow_fallback:
            fallback = shutil.which("python3") or shutil.which("python") or "python"
            return (
                PythonResolution(
                    cmd=[fallback],
                    source="path_fallback",
                    warning=f"Using fallback python from PATH: {fallback}",
                    report_path=str(report_path),
                ),
                None,
            )
        return None, "missing_report"

    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        if allow_fallback:
            fallback = shutil.which("python3") or shutil.which("python") or "python"
            return (
                PythonResolution(
                    cmd=[fallback],
                    source="path_fallback",
                    warning=f"Report missing python_path; using fallback python from PATH: {fallback}",
                    report_path=str(report_path),
                ),
                None,
            )
        return None, "missing_report"

    cmd = _parse_python_cmd(python_path)
    if not cmd:
        return None, "missing_report"
    return (
        PythonResolution(cmd=cmd, source="report", report_path=str(report_path)),
        None,
    )


def python_cmd_to_str(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def probe_python(python_cmd: Sequence[str]) -> Tuple[bool, str, str]:
    """Return (ok, executable, version)."""
    try:
        exe = (
            subprocess.check_output(
                list(python_cmd) + ["-c", "import sys; print(sys.executable)"],
                stderr=subprocess.STDOUT,
            )
            .decode("utf-8", errors="replace")
            .strip()
        )
    except Exception as e:
        return False, "", f"failed_get_executable: {e}"

    try:
        ver = (
            subprocess.check_output(
                list(python_cmd)
                + ["-c", "import platform; print(platform.python_version())"],
                stderr=subprocess.STDOUT,
            )
            .decode("utf-8", errors="replace")
            .strip()
        )
    except Exception as e:
        return False, exe, f"failed_get_version: {e}"

    return True, exe, ver


def default_timeout_for_stage(stage: str) -> int:
    return {
        "prepare": 1200,
        "cpu": 600,
        "cuda": 120,
        "single_gpu": 600,
        "multi_gpu": 1200,
        "env_size": 120,
        "hallucination": 120,
        "pyright": 600,
        "summary": 120,
    }.get(stage, 600)


def base_assets() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def write_results_json(path: Path, payload: dict) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_command_capture(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    timeout_sec: int,
) -> Tuple[int, bool, str]:
    """Return (returncode, timed_out, combined_output_text)."""
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError as e:
        return 127, False, f"Command not found: {e}\n"
    except Exception as e:
        return 127, False, f"Failed to start command: {e}\n"

    try:
        out, _ = proc.communicate(timeout=timeout_sec)
        return proc.returncode, False, out or ""
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        out, _ = proc.communicate()
        return 124, True, (out or "")


def build_env(extra_env: List[str]) -> Dict[str, str]:
    env = os.environ.copy()
    for item in extra_env:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        env[k] = v
    return env


def pick_env_vars_to_record(env: Dict[str, str]) -> Dict[str, str]:
    keys = [
        "SCIMLOPSBENCH_PYTHON",
        "SCIMLOPSBENCH_REPORT",
        "CUDA_VISIBLE_DEVICES",
        "USE_GPU",
        "DEVICE_TYPE",
        "MODEL_DIR",
        "VOICES_DIR",
        "TEMP_FILE_DIR",
        "TMPDIR",
        "PYTHONPATH",
        "PIP_CACHE_DIR",
        "HF_HOME",
        "TRANSFORMERS_CACHE",
        "XDG_CACHE_HOME",
    ]
    out: Dict[str, str] = {}
    for k in keys:
        if k in env:
            out[k] = env[k]
    return out


def cmd_to_shell_string(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def merge_non_reserved(base: dict, extra: dict) -> dict:
    reserved = {
        "status",
        "skip_reason",
        "exit_code",
        "stage",
        "task",
        "command",
        "timeout_sec",
        "framework",
        "assets",
        "meta",
        "failure_category",
        "error_excerpt",
    }
    merged = dict(base)
    for k, v in extra.items():
        if k in reserved:
            continue
        merged[k] = v
    return merged


def main_python_path(args: argparse.Namespace) -> int:
    report_path = resolve_report_path(args.report_path)
    res, err = resolve_python_cmd(
        cli_python=args.python, report_path=report_path, allow_fallback=args.allow_fallback
    )
    if res is None:
        print(f"ERROR: failed_resolve_python: {err}", file=sys.stderr)
        return 1
    print(python_cmd_to_str(res.cmd))
    return 0


def main_run(args: argparse.Namespace) -> int:
    root = repo_root()
    out_dir = (root / args.out_dir).resolve() if not os.path.isabs(args.out_dir) else Path(args.out_dir)
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)

    python_res: Optional[PythonResolution] = None
    python_ok = False
    python_exe = ""
    python_ver = ""

    if args.requires_python:
        python_res, err = resolve_python_cmd(
            cli_python=args.python, report_path=report_path, allow_fallback=args.allow_fallback
        )
        if python_res is None:
            text = f"Failed to resolve python (requires_python=true). report_path={report_path} err={err}\n"
            log_path.write_text(text, encoding="utf-8")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": args.stage,
                "task": args.task,
                "command": "",
                "timeout_sec": int(args.timeout_sec),
                "framework": args.framework,
                "assets": base_assets(),
                "meta": {
                    "python": "",
                    "git_commit": get_git_commit(root),
                    "env_vars": pick_env_vars_to_record(os.environ.copy()),
                    "decision_reason": args.decision_reason or "",
                    "python_resolution_source": "",
                    "python_resolution_warning": "",
                    "report_path": str(report_path),
                },
                "failure_category": "missing_report" if err == "missing_report" else "unknown",
                "error_excerpt": tail_lines(text),
            }
            write_results_json(results_path, payload)
            return 1

        python_ok, python_exe, python_ver = probe_python(python_res.cmd)

    timeout_sec = int(args.timeout_sec) if args.timeout_sec is not None else default_timeout_for_stage(args.stage)

    if args.skip:
        msg = f"SKIPPED stage={args.stage} reason={args.skip_reason} note={args.skip_note}\n"
        log_path.write_text(msg, encoding="utf-8")
        payload = {
            "status": "skipped",
            "skip_reason": args.skip_reason,
            "exit_code": 0,
            "stage": args.stage,
            "task": args.task,
            "command": args.command_str or "",
            "timeout_sec": timeout_sec,
            "framework": args.framework,
            "assets": base_assets(),
            "meta": {
                "python": python_cmd_to_str(python_res.cmd) if python_res else "",
                "git_commit": get_git_commit(root),
                "env_vars": pick_env_vars_to_record(os.environ.copy()),
                "decision_reason": args.decision_reason or "",
                "python_resolution_source": python_res.source if python_res else "",
                "python_resolution_warning": python_res.warning if python_res else "",
                "python_probe_ok": python_ok,
                "python_executable": python_exe,
                "python_version": python_ver,
                "report_path": str(report_path),
                "timestamp_utc": utc_timestamp(),
            },
            "failure_category": "",
            "error_excerpt": "",
        }
        if args.merge_json_path:
            extra, _ = load_json_file(Path(args.merge_json_path))
            if isinstance(extra, dict):
                if isinstance(extra.get("assets"), dict):
                    payload["assets"] = extra["assets"]
                if isinstance(extra.get("meta"), dict):
                    payload["meta"].update(extra["meta"])
                payload = merge_non_reserved(payload, extra)
        write_results_json(results_path, payload)
        return 0

    # Build command.
    cmd: List[str] = []
    if args.command_str:
        cmd = ["bash", "-lc", args.command_str]
    else:
        if not args.cmd:
            text = "No command provided. Use --command-str or provide command after '--'.\n"
            log_path.write_text(text, encoding="utf-8")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": args.stage,
                "task": args.task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": args.framework,
                "assets": base_assets(),
                "meta": {
                    "python": python_cmd_to_str(python_res.cmd) if python_res else "",
                    "git_commit": get_git_commit(root),
                    "env_vars": pick_env_vars_to_record(os.environ.copy()),
                    "decision_reason": args.decision_reason or "",
                    "python_resolution_source": python_res.source if python_res else "",
                    "python_resolution_warning": python_res.warning if python_res else "",
                    "python_probe_ok": python_ok,
                    "python_executable": python_exe,
                    "python_version": python_ver,
                    "report_path": str(report_path),
                    "timestamp_utc": utc_timestamp(),
                },
                "failure_category": "args_unknown",
                "error_excerpt": tail_lines(text),
            }
            write_results_json(results_path, payload)
            return 1
        cmd = list(args.cmd)

    # Environment for command.
    env = build_env(args.env)
    if python_res:
        env["SCIMLOPSBENCH_RESOLVED_PYTHON"] = python_cmd_to_str(python_res.cmd)

    cmd_str = args.command_str or cmd_to_shell_string(cmd)

    rc, timed_out, out = run_command_capture(
        cmd,
        cwd=root,
        env=env,
        timeout_sec=timeout_sec,
    )
    log_path.write_text(out, encoding="utf-8")

    command_exit_code = rc

    status = "success"
    exit_code = 0
    failure_category = ""
    if timed_out:
        status = "failure"
        exit_code = 1
        failure_category = "timeout"
    elif rc != 0 and not args.allow_nonzero_exit:
        status = "failure"
        exit_code = 1
        failure_category = "runtime"

    payload: dict = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": args.stage,
        "task": args.task,
        "command": cmd_str,
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": base_assets(),
        "meta": {
            "python": python_cmd_to_str(python_res.cmd) if python_res else "",
            "git_commit": get_git_commit(root),
            "env_vars": pick_env_vars_to_record(env),
            "decision_reason": args.decision_reason or "",
            "python_resolution_source": python_res.source if python_res else "",
            "python_resolution_warning": python_res.warning if python_res else "",
            "python_probe_ok": python_ok,
            "python_executable": python_exe,
            "python_version": python_ver,
            "report_path": str(report_path),
            "timestamp_utc": utc_timestamp(),
            "command_exit_code": command_exit_code,
        },
        "failure_category": failure_category,
        "error_excerpt": tail_lines(out),
    }

    if args.merge_json_path:
        extra, _ = load_json_file(Path(args.merge_json_path))
        if isinstance(extra, dict):
            if isinstance(extra.get("assets"), dict):
                payload["assets"] = extra["assets"]
            if isinstance(extra.get("meta"), dict):
                payload["meta"].update(extra["meta"])
            if isinstance(extra.get("failure_category"), str) and status == "failure":
                payload["failure_category"] = extra["failure_category"]
            payload = merge_non_reserved(payload, extra)

    write_results_json(results_path, payload)
    return 0 if status in {"success", "skipped"} else 1


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runner.py")
    sub = p.add_subparsers(dest="subcmd", required=True)

    p_py = sub.add_parser("python-path", help="Print resolved python command")
    p_py.add_argument("--report-path", default=None)
    p_py.add_argument("--python", default=None)
    p_py.add_argument("--allow-fallback", action="store_true")
    p_py.set_defaults(func=main_python_path)

    p_run = sub.add_parser("run", help="Run a command and write build_output/<stage>/{log,results}.")
    p_run.add_argument("--stage", required=True)
    p_run.add_argument("--task", required=True)
    p_run.add_argument("--framework", default="unknown")
    p_run.add_argument("--out-dir", required=True)
    p_run.add_argument("--timeout-sec", type=int, default=None)
    p_run.add_argument("--decision-reason", default=None)

    p_run.add_argument("--report-path", default=None)
    p_run.add_argument("--python", default=None)
    p_run.add_argument("--allow-fallback", action="store_true")
    p_run.add_argument("--requires-python", action="store_true", default=True)

    p_run.add_argument("--env", action="append", default=[], help="KEY=VALUE pairs")
    p_run.add_argument("--allow-nonzero-exit", action="store_true")

    p_run.add_argument("--merge-json-path", default=None, help="Optional JSON file to merge into results.")

    p_run.add_argument("--skip", action="store_true")
    p_run.add_argument(
        "--skip-reason",
        default="unknown",
        choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"],
    )
    p_run.add_argument("--skip-note", default="")
    p_run.add_argument("--command-str", default=None, help="Run via bash -lc <command-str> (alternative to -- <cmd...>)")

    p_run.add_argument("cmd", nargs=argparse.REMAINDER)
    p_run.set_defaults(func=main_run)

    return p


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

