#!/usr/bin/env python3
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"

DEFAULT_TIMEOUTS_SEC: dict[str, int] = {
    "prepare": 1200,
    "cpu": 600,
    "cuda": 120,
    "single_gpu": 600,
    "multi_gpu": 1200,
    "env_size": 120,
    "hallucination": 120,
    "pyright": 600,
    "summary": 120,
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = _read_text(path).splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
        )
        return out.strip()
    except Exception:
        return ""


def _env_snapshot(keys: list[str]) -> dict[str, str]:
    snap: dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            snap[k] = v
    return snap


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def _load_report(report_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = report_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception:
        return None, "missing_report"
    try:
        data = json.loads(raw)
    except Exception:
        return None, "missing_report"
    if not isinstance(data, dict):
        return None, "missing_report"
    return data, None


@dataclass(frozen=True)
class PythonResolution:
    python_path: str | None
    source: str
    warning: str | None = None
    report_path: str | None = None


def resolve_python(
    *,
    cli_python: str | None,
    requires_python: bool,
    report_path: Path,
) -> tuple[PythonResolution | None, str | None, str | None]:
    """
    Returns: (resolution, failure_category, error_message)
    """
    if not requires_python:
        return PythonResolution(python_path=None, source="not_required"), None, None

    if cli_python:
        if not Path(cli_python).exists():
            return None, "path_hallucination", f"--python does not exist: {cli_python}"
        if not os.access(cli_python, os.X_OK):
            return None, "path_hallucination", f"--python is not executable: {cli_python}"
        return PythonResolution(python_path=cli_python, source="cli", report_path=str(report_path)), None, None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        if not Path(env_python).exists():
            return None, "path_hallucination", f"SCIMLOPSBENCH_PYTHON does not exist: {env_python}"
        if not os.access(env_python, os.X_OK):
            return None, "path_hallucination", f"SCIMLOPSBENCH_PYTHON is not executable: {env_python}"
        return PythonResolution(python_path=env_python, source="env:SCIMLOPSBENCH_PYTHON", report_path=str(report_path)), None, None

    report, err = _load_report(report_path)
    if err is not None:
        return None, "missing_report", f"Report missing/invalid at: {report_path}"

    reported_python = report.get("python_path")
    if not isinstance(reported_python, str) or not reported_python.strip():
        fallback = shutil.which("python3") or shutil.which("python")
        if not fallback:
            return None, "path_hallucination", "python_path missing in report and no python found on PATH"
        return (
            PythonResolution(
                python_path=fallback,
                source="path_fallback",
                warning="python_path missing in report; using python from PATH",
                report_path=str(report_path),
            ),
            None,
            None,
        )

    reported_python = reported_python.strip()
    if not Path(reported_python).exists():
        return None, "path_hallucination", f"python_path from report does not exist: {reported_python}"
    if not os.access(reported_python, os.X_OK):
        return None, "path_hallucination", f"python_path from report is not executable: {reported_python}"

    return PythonResolution(python_path=reported_python, source="report:python_path", report_path=str(report_path)), None, None


def _cmd_to_string(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _load_assets_from(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_read_text(path))
        assets = data.get("assets", {})
        if isinstance(assets, dict):
            return assets
    except Exception:
        pass
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _base_results(
    *,
    stage: str,
    task: str,
    command: str,
    timeout_sec: int,
    framework: str,
    assets: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": task,
        "command": command,
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": assets,
        "meta": meta,
        "failure_category": "unknown",
        "error_excerpt": "",
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark executor for scimlopsbench stages.")
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_resolve = sub.add_parser("resolve-python", help="Resolve the Python interpreter path for the benchmark.")
    p_resolve.add_argument("--report-path", default=None)
    p_resolve.add_argument("--python", default=None)
    p_resolve.add_argument("--requires-python", dest="requires_python", action="store_true", default=True)
    p_resolve.add_argument("--no-requires-python", dest="requires_python", action="store_false")

    p_run = sub.add_parser("run", help="Run a command and write build_output/<stage>/{log.txt,results.json}.")
    p_run.add_argument("--stage", required=True)
    p_run.add_argument("--task", required=True)
    p_run.add_argument("--framework", default="unknown")
    p_run.add_argument("--out-dir", default=None)
    p_run.add_argument("--timeout-sec", type=int, default=None)
    p_run.add_argument("--report-path", default=None)
    p_run.add_argument("--python", default=None)
    p_run.add_argument("--requires-python", dest="requires_python", action="store_true", default=True)
    p_run.add_argument("--no-requires-python", dest="requires_python", action="store_false")
    p_run.add_argument("--assets-from", default=None, help="Path to a JSON file containing an 'assets' object.")
    p_run.add_argument("--decision-reason", default="")
    p_run.add_argument("--skip", action="store_true", help="Write skipped results.json and exit 0.")
    p_run.add_argument(
        "--skip-reason",
        default="repo_not_supported",
        help="repo_not_supported|insufficient_hardware|not_applicable|unknown",
    )
    p_run.add_argument("--fail", action="store_true", help="Write failure results.json without running.")
    p_run.add_argument("--failure-category", default="unknown")
    p_run.add_argument("--error-message", default="")
    p_run.add_argument("cmd", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    repo_root = _repo_root()

    if args.subcmd == "resolve-python":
        report_path = _resolve_report_path(args.report_path)
        res, failure_category, err_msg = resolve_python(
            cli_python=args.python,
            requires_python=bool(args.requires_python),
            report_path=report_path,
        )
        if not bool(args.requires_python):
            print("")
            return 0
        if failure_category is not None or res is None or res.python_path is None:
            if err_msg:
                print(err_msg, file=sys.stderr)
            return 1
        print(res.python_path)
        return 0

    stage: str = args.stage
    task: str = args.task
    out_dir = Path(args.out_dir) if args.out_dir else repo_root / "build_output" / stage
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = int(args.timeout_sec) if args.timeout_sec is not None else int(DEFAULT_TIMEOUTS_SEC.get(stage, 600))

    report_path = _resolve_report_path(args.report_path)
    py_res, py_fail_cat, py_err = resolve_python(
        cli_python=args.python,
        requires_python=bool(args.requires_python),
        report_path=report_path,
    )

    env_keys = [
        "CUDA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "HF_DATASETS_CACHE",
        "HF_ENDPOINT",
        "XDG_CACHE_HOME",
    ]

    assets: dict[str, Any]
    if args.assets_from:
        assets = _load_assets_from(Path(args.assets_from))
    else:
        assets = {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }

    meta: dict[str, Any] = {
        "python": (py_res.python_path if py_res and py_res.python_path else ""),
        "git_commit": _git_commit(repo_root),
        "env_vars": _env_snapshot(env_keys),
        "decision_reason": args.decision_reason,
        "timestamp_utc": _utc_now_iso(),
        "python_resolution": {
            "source": (py_res.source if py_res else ""),
            "report_path": (py_res.report_path if py_res else str(report_path)),
            "warning": (py_res.warning if py_res else None),
        },
    }

    # Command parsing
    cmd: list[str] = []
    if args.cmd:
        cmd = list(args.cmd)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]

    # Preflight skip/fail modes
    if args.skip or args.fail:
        status = "skipped" if args.skip else "failure"
        exit_code = 0 if args.skip else 1
        failure_category = "unknown" if args.skip else args.failure_category
        command_str = _cmd_to_string(cmd) if cmd else ""
        payload = _base_results(
            stage=stage,
            task=task,
            command=command_str,
            timeout_sec=timeout_sec,
            framework=args.framework,
            assets=assets,
            meta=meta,
        )
        payload["status"] = status
        payload["exit_code"] = exit_code
        payload["skip_reason"] = args.skip_reason if args.skip else "unknown"
        payload["failure_category"] = failure_category
        payload["error_excerpt"] = args.error_message
        _write_text(log_path, (args.error_message + "\n") if args.error_message else "")
        _write_text(results_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return exit_code

    # Report/python resolution failure for python-required stages
    if py_fail_cat is not None:
        payload = _base_results(
            stage=stage,
            task=task,
            command=_cmd_to_string(cmd) if cmd else "",
            timeout_sec=timeout_sec,
            framework=args.framework,
            assets=assets,
            meta=meta,
        )
        payload["status"] = "failure"
        payload["exit_code"] = 1
        payload["failure_category"] = py_fail_cat
        payload["error_excerpt"] = py_err or ""
        _write_text(log_path, (py_err or "") + "\n")
        _write_text(results_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 1

    # Substitute __PYTHON__ token, if present.
    if py_res and py_res.python_path:
        cmd = [py_res.python_path if c == "__PYTHON__" else c for c in cmd]

    command_str = _cmd_to_string(cmd)
    payload = _base_results(
        stage=stage,
        task=task,
        command=command_str,
        timeout_sec=timeout_sec,
        framework=args.framework,
        assets=assets,
        meta=meta,
    )

    if not cmd:
        payload["failure_category"] = "args_unknown"
        payload["error_excerpt"] = "No command provided to runner."
        _write_text(log_path, payload["error_excerpt"] + "\n")
        _write_text(results_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 1

    # Execute
    start = time.time()
    try:
        with open(log_path, "wb") as log_f:
            log_f.write(f"[runner] start_utc={_utc_now_iso()}\n".encode("utf-8"))
            log_f.write(f"[runner] cwd={repo_root}\n".encode("utf-8"))
            log_f.write(f"[runner] cmd={command_str}\n".encode("utf-8"))
            log_f.flush()

            proc = subprocess.Popen(
                cmd,
                cwd=str(repo_root),
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=False,
            )
            try:
                rc = proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = 124
                payload["failure_category"] = "timeout"
                payload["error_excerpt"] = _tail_lines(log_path)
                payload["status"] = "failure"
                payload["exit_code"] = 1
                meta["duration_sec"] = round(time.time() - start, 3)
                _write_text(results_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                return 1
    except FileNotFoundError as e:
        payload["failure_category"] = "entrypoint_not_found"
        payload["error_excerpt"] = str(e)
        _write_text(log_path, str(e) + "\n")
        _write_text(results_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 1
    except Exception as e:
        payload["failure_category"] = "runtime"
        payload["error_excerpt"] = str(e)
        _write_text(log_path, str(e) + "\n")
        _write_text(results_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 1

    meta["duration_sec"] = round(time.time() - start, 3)

    if rc == 0:
        payload["status"] = "success"
        payload["exit_code"] = 0
        payload["skip_reason"] = "not_applicable"
        payload["failure_category"] = "unknown"
        payload["error_excerpt"] = ""
        _write_text(results_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0

    payload["status"] = "failure"
    payload["exit_code"] = 1
    payload["skip_reason"] = "unknown"

    excerpt = _tail_lines(log_path)
    payload["error_excerpt"] = excerpt

    def classify_failure(text: str) -> str:
        lower = text.lower()

        if "cuda out of memory" in lower or "out of memory" in lower or "std::bad_alloc" in lower:
            return "oom"

        if "modulenotfounderror" in lower or "no module named" in lower or "importerror" in lower:
            return "deps"

        if "unrecognized arguments" in lower or "unknown argument" in lower or "error: the following arguments are required" in lower:
            return "args_unknown"

        if "401" in lower or "403" in lower or "unauthorized" in lower or "forbidden" in lower:
            return "auth_required"

        if (
            "temporary failure in name resolution" in lower
            or "name or service not known" in lower
            or "connectionerror" in lower
            or "failed to establish a new connection" in lower
            or "max retries exceeded" in lower
            or "read timed out" in lower
            or "proxyerror" in lower
            or "sslerror" in lower
        ):
            return "download_failed"

        return "runtime"

    payload["failure_category"] = classify_failure(excerpt)

    _write_text(results_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
