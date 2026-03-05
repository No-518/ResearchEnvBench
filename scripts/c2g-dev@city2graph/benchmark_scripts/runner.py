#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_load(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing_file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid_json: {path}: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"read_error: {path}: {e}"


def _git_commit(repo_root: Path) -> str | None:
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return None
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        commit = res.stdout.strip()
        return commit or None
    except Exception:  # noqa: BLE001
        return None


def _is_executable_file(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(path, os.X_OK)
    except Exception:  # noqa: BLE001
        return False


def resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def resolve_python(
    *,
    cli_python: str | None,
    report_path: Path,
    require_report_if_missing: bool,
) -> tuple[str | None, dict[str, Any], list[str]]:
    """Resolve python executable path using required priority.

    Returns: (python_path_or_none, report_data, warnings)
    """
    warnings: list[str] = []

    if cli_python:
        return cli_python, {}, warnings

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return env_python, {}, warnings

    report_data, report_err = _json_load(report_path)
    if report_err is not None:
        if require_report_if_missing:
            return None, {}, [report_err]
        report_data = None

    python_path: str | None = None
    if isinstance(report_data, dict):
        val = report_data.get("python_path")
        if isinstance(val, str) and val.strip():
            python_path = val.strip()

    if python_path:
        return python_path, (report_data or {}), warnings

    # Fallback python from PATH (allowed only if report exists/valid OR report not required)
    fallback = shutil.which("python3") or shutil.which("python")
    if fallback:
        warnings.append("Using fallback python from PATH (report python_path missing/empty).")
        return fallback, (report_data or {}), warnings

    return None, (report_data or {}), warnings


def _safe_env_snapshot() -> dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "PYTHONPATH",
        "PATH",
        "HF_AUTH_TOKEN",
        "HF_TOKEN",
        "TRANSFORMERS_CACHE",
        "HF_HOME",
        "XDG_CACHE_HOME",
    ]
    out: dict[str, str] = {}
    for k in keys:
        if k not in os.environ:
            continue
        v = os.environ.get(k, "")
        if any(s in k.upper() for s in ("TOKEN", "SECRET", "KEY", "PASS")) and v:
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out


def _default_timeout_for_stage(stage: str) -> int:
    defaults: dict[str, int] = {
        "prepare": 1200,
        "cpu": 600,
        "cuda": 600,
        "single_gpu": 600,
        "multi_gpu": 1200,
        "env_size": 120,
        "hallucination": 120,
        "pyright": 600,
        "summary": 120,
    }
    return defaults.get(stage, 600)


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


class _StreamTee:
    def __init__(
        self,
        *,
        stream_name: str,
        stream: Any,
        log_fh: Any,
        tee_fh: Any | None,
        tail: deque[str],
        tail_lock: threading.Lock,
    ) -> None:
        self._stream_name = stream_name
        self._stream = stream
        self._log_fh = log_fh
        self._tee_fh = tee_fh
        self._tail = tail
        self._tail_lock = tail_lock

    def run(self) -> None:
        prefix = f"[{self._stream_name}] ".encode("utf-8")
        for raw in iter(self._stream.readline, b""):
            try:
                self._log_fh.write(prefix + raw)
                self._log_fh.flush()
            except Exception:  # noqa: BLE001
                pass
            if self._tee_fh is not None:
                try:
                    self._tee_fh.write(raw)
                    self._tee_fh.flush()
                except Exception:  # noqa: BLE001
                    pass
            try:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                with self._tail_lock:
                    self._tail.append(line)
            except Exception:  # noqa: BLE001
                pass


def run_command(
    *,
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_sec: int,
    log_path: Path,
    stdout_tee_path: Path | None = None,
    stderr_tee_path: Path | None = None,
) -> tuple[int, bool, str]:
    """Run a command with timeout. Returns (exit_code, timed_out, error_excerpt)."""
    _ensure_dir(log_path.parent)
    tail: deque[str] = deque(maxlen=220)
    tail_lock = threading.Lock()

    with log_path.open("ab") as log_fh:
        header = (
            f"\n===== runner.py { _now_utc_iso() } =====\n"
            f"cwd: {cwd}\n"
            f"cmd: {shlex.join(cmd)}\n"
        ).encode("utf-8")
        log_fh.write(header)
        log_fh.flush()

        stdout_fh = stdout_tee_path.open("ab") if stdout_tee_path is not None else None
        stderr_fh = stderr_tee_path.open("ab") if stderr_tee_path is not None else None
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as e:  # noqa: BLE001
            log_fh.write(f"[runner_error] Failed to start process: {e}\n".encode("utf-8"))
            log_fh.flush()
            if stdout_fh:
                stdout_fh.close()
            if stderr_fh:
                stderr_fh.close()
            return 1, False, f"Failed to start process: {e}"

        assert proc.stdout is not None
        assert proc.stderr is not None

        t_out = _StreamTee(
            stream_name="stdout",
            stream=proc.stdout,
            log_fh=log_fh,
            tee_fh=stdout_fh,
            tail=tail,
            tail_lock=tail_lock,
        )
        t_err = _StreamTee(
            stream_name="stderr",
            stream=proc.stderr,
            log_fh=log_fh,
            tee_fh=stderr_fh,
            tail=tail,
            tail_lock=tail_lock,
        )

        th_out = threading.Thread(target=t_out.run, daemon=True)
        th_err = threading.Thread(target=t_err.run, daemon=True)
        th_out.start()
        th_err.start()

        timed_out = False
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                log_fh.write(b"[runner_timeout] Timeout exceeded; terminating process.\n")
                log_fh.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

        th_out.join(timeout=5)
        th_err.join(timeout=5)

        if stdout_fh:
            stdout_fh.close()
        if stderr_fh:
            stderr_fh.close()

        exit_code = int(proc.returncode or 0)
        if timed_out:
            exit_code = 1

        with tail_lock:
            excerpt = "\n".join(list(tail)[-220:])
        return exit_code, timed_out, excerpt[-8000:]


def write_results(results_path: Path, payload: dict[str, Any]) -> None:
    _ensure_dir(results_path.parent)
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Unified benchmark runner (writes log.txt/results.json).")
    p.add_argument("--stage", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--framework", default="unknown")
    p.add_argument("--timeout-sec", type=int, default=None)
    p.add_argument("--python", dest="cli_python", default=None)
    p.add_argument("--report-path", default=None)
    p.add_argument("--require-python", action="store_true")
    p.add_argument("--skip", action="store_true")
    p.add_argument("--skip-reason", default="unknown")
    p.add_argument("--failure-category", default="unknown")
    p.add_argument("--decision-reason", default="")
    p.add_argument("--stdout-tee", default=None)
    p.add_argument("--stderr-tee", default=None)
    p.add_argument("--context", default=None, help="Optional JSON file merged into results payload.")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run (pass after --).")
    args = p.parse_args(argv)

    repo_root = _repo_root()
    out_dir = Path(args.out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = args.timeout_sec if args.timeout_sec is not None else _default_timeout_for_stage(args.stage)

    report_path = resolve_report_path(args.report_path)
    resolved_python, report_data, python_warnings = resolve_python(
        cli_python=args.cli_python,
        report_path=report_path,
        require_report_if_missing=args.require_python,
    )

    context: dict[str, Any] = {}
    if args.context:
        ctx, ctx_err = _json_load(Path(args.context))
        if ctx_err is None and isinstance(ctx, dict):
            context = ctx

    base_assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

    payload: dict[str, Any] = {
        "status": "failure",
        "skip_reason": args.skip_reason,
        "exit_code": 1,
        "stage": args.stage,
        "task": args.task,
        "command": "",
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": base_assets,
        "meta": {
            "python": resolved_python or "",
            "git_commit": _git_commit(repo_root) or "",
            "env_vars": _safe_env_snapshot(),
            "decision_reason": args.decision_reason,
            "timestamp_utc": _now_utc_iso(),
            "python_resolution_warnings": python_warnings,
            "report_path": str(report_path),
        },
        "failure_category": args.failure_category,
        "error_excerpt": "",
    }

    # Merge user context last (but keep required keys present).
    # Context may include `assets`, `meta` additions, etc.
    for k, v in context.items():
        if k == "assets" and isinstance(v, dict):
            payload["assets"].update(v)
        elif k == "meta" and isinstance(v, dict):
            payload["meta"].update(v)
        else:
            payload[k] = v

    if args.skip:
        payload["status"] = "skipped"
        payload["skip_reason"] = args.skip_reason
        payload["exit_code"] = 0
        payload["failure_category"] = "unknown"
        payload["command"] = payload.get("command") or "skipped"
        payload["error_excerpt"] = ""
        write_results(results_path, payload)
        _ensure_dir(log_path.parent)
        if not log_path.exists():
            log_path.write_text("skipped\n", encoding="utf-8")
        return 0

    if args.require_python and resolved_python is None:
        payload["status"] = "failure"
        payload["exit_code"] = 1
        payload["failure_category"] = "missing_report"
        payload["error_excerpt"] = "No python could be resolved (missing/invalid report and no override)."
        write_results(results_path, payload)
        _ensure_dir(log_path.parent)
        log_path.write_text(payload["error_excerpt"] + "\n", encoding="utf-8")
        return 1

    cmd = [c for c in args.cmd if c != "--"]
    if not cmd:
        payload["status"] = "failure"
        payload["exit_code"] = 1
        payload["failure_category"] = "args_unknown"
        payload["error_excerpt"] = "No command provided."
        write_results(results_path, payload)
        _ensure_dir(log_path.parent)
        log_path.write_text(payload["error_excerpt"] + "\n", encoding="utf-8")
        return 1

    payload["command"] = shlex.join(cmd)

    env = os.environ.copy()
    exit_code, timed_out, excerpt = run_command(
        cmd=cmd,
        cwd=repo_root,
        env=env,
        timeout_sec=timeout_sec,
        log_path=log_path,
        stdout_tee_path=Path(args.stdout_tee) if args.stdout_tee else None,
        stderr_tee_path=Path(args.stderr_tee) if args.stderr_tee else None,
    )

    payload["exit_code"] = 0 if exit_code == 0 else 1
    payload["status"] = "success" if exit_code == 0 else "failure"
    if timed_out:
        payload["failure_category"] = "timeout"
    elif exit_code != 0 and payload.get("failure_category") == "unknown":
        payload["failure_category"] = "runtime"
    payload["error_excerpt"] = excerpt

    write_results(results_path, payload)
    return 0 if payload["status"] in ("success", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())

