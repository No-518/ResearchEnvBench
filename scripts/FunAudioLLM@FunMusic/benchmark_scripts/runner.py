#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


class RunnerError(Exception):
    pass


class MissingReportError(RunnerError):
    pass


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _tail_lines(path: Path, max_lines: int) -> str:
    if not path.exists():
        return ""
    try:
        text = _read_text(path)
    except Exception:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[-max_lines:])


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _git_commit(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.DEVNULL, text=True
        ).strip()
        return out or None
    except Exception:
        return None


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def _load_report_json(report_path: Path) -> Dict[str, Any]:
    if not report_path.exists():
        raise MissingReportError(f"Report not found: {report_path}")
    try:
        return json.loads(_read_text(report_path))
    except json.JSONDecodeError as e:
        raise MissingReportError(f"Report is not valid JSON: {report_path} ({e})") from e


def _is_executable_file(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def _resolve_python(
    *,
    cli_python: Optional[str],
    requires_python: bool,
    report_path: Path,
) -> Tuple[Optional[str], Dict[str, Any]]:
    meta: Dict[str, Any] = {"resolution_source": None, "warnings": []}

    if not requires_python:
        return None, meta

    if cli_python:
        meta["resolution_source"] = "cli"
        return cli_python, meta

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        meta["resolution_source"] = "env"
        return env_python, meta

    report = _load_report_json(report_path)
    reported_python = report.get("python_path")
    meta["reported_python_path"] = reported_python
    if isinstance(reported_python, str) and _is_executable_file(Path(reported_python)):
        meta["resolution_source"] = "report"
        return reported_python, meta

    fallback = shutil.which("python") or shutil.which("python3")
    if fallback:
        meta["resolution_source"] = "path_fallback"
        meta["warnings"].append(
            "Report python_path missing/invalid; falling back to python from PATH. "
            "Stage results may not reflect the agent environment."
        )
        meta["fallback_python_path"] = fallback
        return fallback, meta

    meta["resolution_source"] = "unresolved"
    raise MissingReportError(
        "Unable to resolve python: report python_path missing/invalid and no python in PATH."
    )


def _python_version(python_exe: str) -> Optional[str]:
    try:
        out = subprocess.check_output(
            [python_exe, "-c", "import platform; print(platform.python_version())"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
        return out or None
    except Exception:
        return None


def _heuristic_failure_category(log_excerpt: str) -> str:
    hay = log_excerpt.lower()
    if "timed out" in hay or "timeout" in hay:
        return "timeout"
    if "out of memory" in hay:
        return "oom"
    if "flash_attn" in hay or "flashattention" in hay or "flash attention" in hay:
        return "deps"
    if "no module named" in hay or "modulenotfounderror" in hay:
        return "deps"
    if "importerror" in hay or "cannot import name" in hay:
        return "deps"
    if "undefined symbol" in hay or "symbol not found" in hay or "libtorch" in hay:
        return "deps"
    if "torch.utils._pytree" in hay or "register_pytree_node" in hay:
        return "deps"
    if "_array_api" in hay or "failed to initialize numpy" in hay:
        return "deps"
    if "cudaexecutionprovider not available" in hay or "onnxruntime-gpu" in hay:
        return "deps"
    if "does not seem to have any of the loading methods defined" in hay and "placeholder" in hay:
        return "deps"
    if "unrecognized arguments" in hay or "unknown argument" in hay:
        return "args_unknown"
    if "permission denied" in hay:
        return "deps"
    if "http" in hay and ("401" in hay or "403" in hay):
        return "auth_required"
    if "connectionerror" in hay or "failed to establish a new connection" in hay:
        return "download_failed"
    if "filenotfounderror" in hay or "no such file or directory" in hay:
        return "data"
    return "runtime"


def _default_timeout(stage: str) -> int:
    defaults = {
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
    return defaults.get(stage, 600)


def _parse_env_overrides(items: List[str]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--env expects KEY=VALUE, got: {item}")
        k, v = item.split("=", 1)
        env[k] = v
    return env


def _build_results_skeleton(
    *,
    stage: str,
    task: str,
    command_str: str,
    timeout_sec: int,
    framework: str,
    assets: Dict[str, Any],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": task,
        "command": command_str,
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": assets,
        "meta": meta,
        "failure_category": "unknown",
        "error_excerpt": "",
    }


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Unified benchmark command runner: executes a command, writes build_output/<stage>/{log.txt,results.json}.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python benchmark_scripts/runner.py --stage cpu --task infer -- -- python inspiremusic/bin/inference.py --help
              python benchmark_scripts/runner.py --stage multi_gpu --task train --timeout-sec 1200 -- -- python -m torch.distributed.run --nproc_per_node 2 ...
            """
        ),
    )
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--framework", default="unknown")
    parser.add_argument("--timeout-sec", type=int)
    parser.add_argument("--out-dir", help="Default: build_output/<stage>")
    parser.add_argument("--assets-from", help="Path to a JSON file containing an 'assets' object (e.g., build_output/prepare/results.json).")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--report-path")
    parser.add_argument("--python", dest="cli_python")
    parser.add_argument("--requires-python", type=int, default=1, help="1/0. If 0, python resolution is skipped.")
    parser.add_argument("--env", action="append", default=[], help="Repeatable KEY=VALUE env var overrides for the command.")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to execute (prefix with -- to separate).")

    args = parser.parse_args(argv)

    repo_root = _repo_root()
    stage = args.stage
    task = args.task
    out_dir = Path(args.out_dir) if args.out_dir else repo_root / "build_output" / stage
    _safe_mkdir(out_dir)

    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    # Normalize command: argparse includes leading "--" in command sometimes.
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    timeout_sec = int(args.timeout_sec) if args.timeout_sec is not None else _default_timeout(stage)
    report_path = _resolve_report_path(args.report_path)
    requires_python = bool(args.requires_python)

    env_overrides: Dict[str, str] = {}
    try:
        env_overrides = _parse_env_overrides(args.env)
    except Exception as e:
        # Early failure: still write results.json + log.txt.
        _safe_mkdir(out_dir)
        err_msg = f"Invalid --env: {e}"
        log_path.write_text(err_msg + "\n", encoding="utf-8")
        meta = {
            "python": None,
            "python_version": None,
            "git_commit": _git_commit(repo_root),
            "env_vars": env_overrides,
            "decision_reason": args.decision_reason,
            "timestamp_utc": _utc_now_iso(),
        }
        results = _build_results_skeleton(
            stage=stage,
            task=task,
            command_str=" ".join(shlex.quote(x) for x in cmd) if cmd else "",
            timeout_sec=timeout_sec,
            framework=args.framework,
            assets={"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
            meta=meta,
        )
        results["failure_category"] = "unknown"
        results["error_excerpt"] = err_msg
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    assets: Dict[str, Any] = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    if args.assets_from:
        try:
            assets_src = json.loads(_read_text(Path(args.assets_from)))
            if isinstance(assets_src, dict) and isinstance(assets_src.get("assets"), dict):
                assets = assets_src["assets"]
        except Exception:
            pass

    python_exe: Optional[str] = None
    python_meta: Dict[str, Any] = {}
    try:
        python_exe, python_meta = _resolve_python(
            cli_python=args.cli_python,
            requires_python=requires_python,
            report_path=report_path,
        )
    except MissingReportError as e:
        err = str(e)
        log_path.write_text(err + "\n", encoding="utf-8")
        meta = {
            "python": None,
            "python_version": None,
            "git_commit": _git_commit(repo_root),
            "env_vars": env_overrides,
            "decision_reason": args.decision_reason,
            "timestamp_utc": _utc_now_iso(),
            "report_path": str(report_path),
            "python_resolution": python_meta,
        }
        results = _build_results_skeleton(
            stage=stage,
            task=task,
            command_str=" ".join(shlex.quote(x) for x in cmd) if cmd else "",
            timeout_sec=timeout_sec,
            framework=args.framework,
            assets=assets,
            meta=meta,
        )
        results["failure_category"] = "missing_report"
        results["error_excerpt"] = err
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    if not cmd:
        log_path.write_text("No command provided.\n", encoding="utf-8")
        meta = {
            "python": python_exe,
            "python_version": _python_version(python_exe) if python_exe else None,
            "git_commit": _git_commit(repo_root),
            "env_vars": env_overrides,
            "decision_reason": args.decision_reason,
            "timestamp_utc": _utc_now_iso(),
            "report_path": str(report_path),
            "python_resolution": python_meta,
        }
        results = _build_results_skeleton(
            stage=stage,
            task=task,
            command_str="",
            timeout_sec=timeout_sec,
            framework=args.framework,
            assets=assets,
            meta=meta,
        )
        results["failure_category"] = "args_unknown"
        results["error_excerpt"] = "No command provided."
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    # Replace a leading "python"/"python3" with the resolved python, to keep stages consistent.
    if requires_python and python_exe and cmd[0] in {"python", "python3"}:
        cmd = [python_exe] + cmd[1:]

    command_str = " ".join(shlex.quote(x) for x in cmd)

    env = os.environ.copy()
    env.update(env_overrides)

    meta = {
        "python": python_exe,
        "python_version": _python_version(python_exe) if python_exe else None,
        "git_commit": _git_commit(repo_root),
        "env_vars": env_overrides,
        "decision_reason": args.decision_reason,
        "timestamp_utc": _utc_now_iso(),
        "report_path": str(report_path),
        "python_resolution": python_meta,
    }

    results = _build_results_skeleton(
        stage=stage,
        task=task,
        command_str=command_str,
        timeout_sec=timeout_sec,
        framework=args.framework,
        assets=assets,
        meta=meta,
    )

    start = time.time()
    timed_out = False
    returncode: Optional[int] = None

    _safe_mkdir(out_dir)
    with open(log_path, "w", encoding="utf-8", errors="replace") as log_f:
        try:
            # Start a new process group so we can kill children on timeout.
            preexec_fn = os.setsid if os.name != "nt" else None
            proc = subprocess.Popen(
                cmd,
                cwd=str(repo_root),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
                preexec_fn=preexec_fn,
            )
            try:
                returncode = proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                timed_out = True
                if os.name != "nt":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        proc.kill()
                else:
                    proc.kill()
                returncode = proc.wait(timeout=10)
        except FileNotFoundError as e:
            log_f.write(f"FileNotFoundError: {e}\n")
            results["failure_category"] = "entrypoint_not_found"
        except Exception as e:
            log_f.write(f"Runner exception: {type(e).__name__}: {e}\n")
            results["failure_category"] = "unknown"

    duration = time.time() - start
    results["meta"]["duration_sec"] = round(duration, 3)

    excerpt = _tail_lines(log_path, max_lines=220)
    results["error_excerpt"] = excerpt

    if timed_out:
        results["status"] = "failure"
        results["exit_code"] = 1
        results["failure_category"] = "timeout"
    elif results.get("failure_category") == "entrypoint_not_found":
        results["status"] = "failure"
        results["exit_code"] = 1
    elif returncode is None:
        results["status"] = "failure"
        results["exit_code"] = 1
        if results["failure_category"] == "unknown":
            results["failure_category"] = "runtime"
    elif returncode == 0:
        results["status"] = "success"
        results["exit_code"] = 0
        results["failure_category"] = "unknown"
        results["error_excerpt"] = ""
    else:
        results["status"] = "failure"
        results["exit_code"] = 1
        results["failure_category"] = _heuristic_failure_category(excerpt)

    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if results["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
