#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_report_path() -> Path:
    return Path("/opt/scimlopsbench/report.json")


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return _default_report_path()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_json_file(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(_read_text(path)), None
    except FileNotFoundError:
        return None, f"report not found: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json: {path}: {e}"
    except Exception as e:
        return None, f"failed reading json: {path}: {e}"


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(str(path), os.X_OK)


def _which_python_from_path() -> Optional[str]:
    return shutil.which("python3") or shutil.which("python")


def resolve_python(
    *,
    cli_python: Optional[str],
    report_path: Path,
    requires_python: bool,
) -> Tuple[Optional[str], Dict[str, Any]]:
    info: Dict[str, Any] = {
        "report_path": str(report_path),
        "resolution": None,
        "warnings": [],
        "report_error": None,
        "report_python_path": None,
        "used_fallback_python": False,
    }

    if cli_python:
        info["resolution"] = "cli"
        info["resolved_python"] = cli_python
        return cli_python, info

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        info["resolution"] = "env:SCIMLOPSBENCH_PYTHON"
        info["resolved_python"] = env_python
        return env_python, info

    report, report_err = _load_json_file(report_path)
    if report is None:
        info["resolution"] = "missing_report"
        info["report_error"] = report_err
        if requires_python:
            return None, info
        fallback = _which_python_from_path()
        if fallback:
            info["used_fallback_python"] = True
            info["warnings"].append("report missing/invalid; using python from PATH")
            info["resolved_python"] = fallback
            return fallback, info
        return None, info

    reported_python = report.get("python_path")
    info["report_python_path"] = reported_python
    if isinstance(reported_python, str) and reported_python:
        p = Path(reported_python)
        if _is_executable_file(p):
            info["resolution"] = "report:python_path"
            info["resolved_python"] = reported_python
            return reported_python, info

    fallback = _which_python_from_path()
    if fallback:
        info["used_fallback_python"] = True
        info["resolution"] = "fallback:PATH"
        info["warnings"].append("report python_path missing/invalid; using python from PATH")
        info["resolved_python"] = fallback
        return fallback, info

    info["resolution"] = "no_python_found"
    return None, info


def _git_commit(repo_root: Path) -> Optional[str]:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if p.returncode == 0:
            return p.stdout.strip() or None
    except Exception:
        return None
    return None


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        with path.open("rb") as f:
            data = f.read()
        lines = data.splitlines()[-max_lines:]
        return b"\n".join(lines).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def _safe_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _command_to_string(command: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in command)


def _parse_env_overrides(items: List[str]) -> Tuple[Dict[str, str], List[str]]:
    env: Dict[str, str] = {}
    warnings: List[str] = []
    for item in items:
        if "=" not in item:
            warnings.append(f"ignoring invalid --env (expected KEY=VAL): {item}")
            continue
        k, v = item.split("=", 1)
        if not k:
            warnings.append(f"ignoring invalid --env (empty key): {item}")
            continue
        env[k] = v
    return env, warnings


def _load_assets_from(path: Optional[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    if not path:
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }, None
    p = Path(path)
    data, err = _load_json_file(p)
    if data is None:
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }, err
    assets = data.get("assets")
    if isinstance(assets, dict):
        return assets, None
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }, "assets missing or invalid in assets-from JSON"


def _python_version(python_exe: str) -> Optional[str]:
    try:
        p = subprocess.run(
            [python_exe, "-c", "import platform; print(platform.python_version())"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        if p.returncode == 0:
            return p.stdout.strip() or None
    except Exception:
        return None
    return None


def _infer_requires_python(command: List[str], explicit: Optional[bool]) -> bool:
    if explicit is not None:
        return explicit
    if not command:
        return False
    head = command[0]
    return head in {"python", "python3"} or head.endswith("/python") or head.endswith("/python3")


def run_command_and_write_results(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    os.chdir(str(repo_root))

    # In print mode, never touch build_output/ to avoid side effects.
    if args.print_python:
        report_path = _resolve_report_path(args.report_path)
        resolved_python, py_info = resolve_python(
            cli_python=args.python,
            report_path=report_path,
            requires_python=True,
        )
        if resolved_python:
            print(resolved_python)
            return 0
        if py_info.get("report_error"):
            print(py_info["report_error"], file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir) if args.out_dir else (repo_root / "build_output" / args.stage)
    out_dir = out_dir if out_dir.is_absolute() else (repo_root / out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    env_overrides, env_warnings = _parse_env_overrides(args.env or [])

    command: List[str] = list(args.command or [])
    requires_python = _infer_requires_python(command, args.requires_python)

    report_path = _resolve_report_path(args.report_path)
    resolved_python, py_info = resolve_python(
        cli_python=args.python,
        report_path=report_path,
        requires_python=requires_python,
    )

    # Build base result skeleton early so we can always write results.json.
    assets, assets_err = _load_assets_from(args.assets_from)
    meta: Dict[str, Any] = {
        "timestamp_utc": _utc_now_iso(),
        "git_commit": _git_commit(repo_root) or "",
        "env_vars": {k: env_overrides.get(k, os.environ.get(k, "")) for k in sorted(set(env_overrides))},
        "decision_reason": args.decision_reason or "",
        "python_resolution": py_info,
        "runner_python": sys.executable,
        "runner_python_version": ".".join(map(str, sys.version_info[:3])),
        "warnings": env_warnings + ([] if assets_err is None else [assets_err]),
    }

    result: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": args.skip_reason or "unknown",
        "exit_code": 1,
        "stage": args.stage,
        "task": args.task,
        "command": "",
        "timeout_sec": int(args.timeout_sec),
        "framework": args.framework,
        "assets": assets,
        "meta": meta,
        "failure_category": args.failure_category or "unknown",
        "error_excerpt": "",
    }

    if args.skip:
        result.update(
            {
                "status": "skipped",
                "exit_code": 0,
                "failure_category": "not_applicable",
                "command": args.command_string or "skipped",
                "error_excerpt": "",
            }
        )
        _safe_write_json(results_path, result)
        log_path.write_text("skipped\n", encoding="utf-8")
        return 0

    if requires_python and not resolved_python:
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = "missing_report"
        result["command"] = args.command_string or _command_to_string(command) or ""
        result["meta"]["python_resolution_error"] = py_info.get("report_error") or "python resolution failed"
        _safe_write_json(results_path, result)
        log_path.write_text(
            (py_info.get("report_error") or "python resolution failed") + "\n",
            encoding="utf-8",
        )
        result["error_excerpt"] = _tail_lines(log_path)
        _safe_write_json(results_path, result)
        return 1

    executed_command = command[:]
    if executed_command and executed_command[0] in {"python", "python3"} and resolved_python:
        executed_command[0] = resolved_python

    env = os.environ.copy()
    env.update(env_overrides)

    # Record python version if we resolved one.
    if resolved_python:
        meta["python"] = resolved_python
        meta["python_version"] = _python_version(resolved_python) or ""
    else:
        meta["python"] = ""
        meta["python_version"] = ""

    result["command"] = args.command_string or _command_to_string(executed_command)

    start = _dt.datetime.now(tz=_dt.timezone.utc)
    meta["start_time_utc"] = start.isoformat()
    meta["cwd"] = str(repo_root)

    # Execute
    try:
        with log_path.open("w", encoding="utf-8") as log_f:
            if not executed_command:
                raise ValueError("no command provided")
            p = subprocess.Popen(
                executed_command,
                cwd=str(repo_root),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                p.wait(timeout=int(args.timeout_sec))
            except subprocess.TimeoutExpired:
                p.kill()
                result["status"] = "failure"
                result["exit_code"] = 1
                result["failure_category"] = "timeout"
                meta["timeout_sec"] = int(args.timeout_sec)
            else:
                if p.returncode == 0:
                    result["status"] = "success"
                    result["exit_code"] = 0
                    result["failure_category"] = "unknown"
                else:
                    result["status"] = "failure"
                    result["exit_code"] = 1
                    if not args.failure_category:
                        result["failure_category"] = "runtime"
                    meta["process_returncode"] = int(p.returncode)
    except FileNotFoundError:
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = "entrypoint_not_found"
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write("command not found\n")
            log_f.write(traceback.format_exc() + "\n")
    except Exception:
        result["status"] = "failure"
        result["exit_code"] = 1
        if not args.failure_category:
            result["failure_category"] = "unknown"
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write("runner exception\n")
            log_f.write(traceback.format_exc() + "\n")

    end = _dt.datetime.now(tz=_dt.timezone.utc)
    meta["end_time_utc"] = end.isoformat()
    meta["duration_sec"] = max(0.0, (end - start).total_seconds())

    result["error_excerpt"] = _tail_lines(log_path)
    _safe_write_json(results_path, result)
    return 0 if result["status"] in {"success", "skipped"} else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Unified executor for benchmark stages.")
    p.add_argument("--stage", required=True, help="Stage name, e.g. cpu|single_gpu|multi_gpu")
    p.add_argument("--task", required=True, help="Task name, e.g. train|infer|check|download|validate")
    p.add_argument("--out-dir", default=None, help="Output directory (default: build_output/<stage>)")
    p.add_argument("--timeout-sec", type=int, default=600, help="Timeout seconds")
    p.add_argument("--framework", default="unknown", help="pytorch|tensorflow|jax|unknown")

    p.add_argument("--report-path", default=None, help="Override report path (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)")
    p.add_argument("--python", default=None, help="Explicit python executable (highest priority)")
    p.add_argument(
        "--requires-python",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="Force python resolution requirement (default: auto if command starts with python/python3)",
    )
    p.add_argument("--env", action="append", default=[], help="Environment override KEY=VAL (repeatable)")

    p.add_argument("--assets-from", default=None, help="Path to a JSON containing an 'assets' object (typically build_output/prepare/results.json)")
    p.add_argument("--decision-reason", default="", help="Short explanation for the chosen entrypoint/params")
    p.add_argument("--failure-category", default=None, help="Override failure_category on non-zero exit")

    p.add_argument("--skip", action="store_true", help="Write skipped results and exit 0")
    p.add_argument("--skip-reason", default="unknown", help="repo_not_supported|insufficient_hardware|not_applicable|unknown")
    p.add_argument("--command-string", default=None, help="Optional command string to record (defaults to joined argv)")
    p.add_argument("--print-python", action="store_true", help="Print resolved python path and exit")

    p.add_argument("command", nargs=argparse.REMAINDER, help="Command to execute (use -- to separate)")
    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return run_command_and_write_results(args)


if __name__ == "__main__":
    raise SystemExit(main())
