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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")

DEFAULT_TIMEOUTS_SEC: Dict[str, int] = {
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


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    fallback = here.parents[1]
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(fallback),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            return Path(out).resolve()
    except Exception:
        pass
    return fallback


def _git_commit(repo_root: Path) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_root),
                text=True,
                stderr=subprocess.DEVNULL,
            )
            .strip()
            .strip()
        )
    except Exception:
        return ""


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text_tail(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    tail = lines[-max_lines:]
    return "\n".join(tail).strip()


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return DEFAULT_REPORT_PATH


def _load_report(report_path: Path) -> Dict[str, Any]:
    return json.loads(report_path.read_text(encoding="utf-8"))


@dataclass
class ResolvedPython:
    python: str
    warnings: List[str]
    source: str


class MissingReportError(RuntimeError):
    pass


class InvalidPythonPathError(RuntimeError):
    pass


def _is_executable_file(path: str) -> bool:
    p = Path(path)
    return p.exists() and p.is_file() and os.access(str(p), os.X_OK)


def resolve_python(
    *,
    cli_python: Optional[str],
    report_path: Path,
    requires_python: bool,
) -> ResolvedPython:
    warnings: List[str] = []

    if cli_python:
        if not _is_executable_file(cli_python):
            raise InvalidPythonPathError(f"--python is not executable: {cli_python}")
        return ResolvedPython(python=cli_python, warnings=warnings, source="cli")

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        if not _is_executable_file(env_python):
            raise InvalidPythonPathError(
                f"$SCIMLOPSBENCH_PYTHON is not executable: {env_python}"
            )
        return ResolvedPython(python=env_python, warnings=warnings, source="env")

    if report_path.exists():
        try:
            report = _load_report(report_path)
        except Exception as e:
            if requires_python:
                raise MissingReportError(f"Invalid report JSON at {report_path}: {e}")
            report = {}
        python_path = report.get("python_path")
        if python_path:
            if not _is_executable_file(python_path):
                raise InvalidPythonPathError(
                    f"python_path from report is not executable: {python_path}"
                )
            return ResolvedPython(
                python=python_path, warnings=warnings, source="report"
            )
        if requires_python:
            raise MissingReportError(
                f"report.json missing 'python_path': {report_path}"
            )
    else:
        if requires_python:
            raise MissingReportError(f"Missing report.json at {report_path}")

    fallback = shutil.which("python3") or shutil.which("python") or "python"
    warnings.append(f"Using fallback python from PATH: {fallback}")
    return ResolvedPython(python=fallback, warnings=warnings, source="path_fallback")


def _default_assets(repo_root: Path) -> Dict[str, Any]:
    return {
        "dataset": {
            "path": str((repo_root / "benchmark_assets" / "dataset").resolve()),
            "source": "not_applicable",
            "version": "unknown",
            "sha256": "",
        },
        "model": {
            "path": str((repo_root / "benchmark_assets" / "model").resolve()),
            "source": "not_applicable",
            "version": "unknown",
            "sha256": "",
        },
    }


def _select_env_vars(env: Dict[str, str]) -> Dict[str, str]:
    keys = [
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "CUDA_VISIBLE_DEVICES",
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "TORCH_HOME",
        "HF_HOME",
        "TRANSFORMERS_CACHE",
        "XDG_CACHE_HOME",
        "PIP_CACHE_DIR",
        "HOME",
        "TMPDIR",
        "MASTER_ADDR",
        "MASTER_PORT",
        "NCCL_DEBUG",
        "NCCL_ASYNC_ERROR_HANDLING",
        "OMP_NUM_THREADS",
    ]
    return {k: env.get(k, "") for k in keys if k in env or k.startswith("SCIMLOPSBENCH")}


def _stage_defaults(stage: str) -> int:
    return DEFAULT_TIMEOUTS_SEC.get(stage, 600)


def _normalize_command_tokens(tokens: List[str]) -> List[str]:
    return [t for t in tokens if t != ""]


def _cmd_to_str(tokens: List[str]) -> str:
    return " ".join(shlex.quote(t) for t in tokens)


def _apply_python_placeholder(tokens: List[str], python_bin: str) -> List[str]:
    return [python_bin if t == "{python}" else t for t in tokens]


def _runner_env(repo_root: Path, base_env: Dict[str, str]) -> Dict[str, str]:
    env = dict(base_env)

    cache_root = repo_root / "benchmark_assets" / "cache"
    _ensure_dir(cache_root)
    _ensure_dir(cache_root / "pip")
    _ensure_dir(cache_root / "torch")
    _ensure_dir(cache_root / "huggingface")
    _ensure_dir(cache_root / "xdg")
    _ensure_dir(cache_root / "home")
    _ensure_dir(cache_root / "tmp")

    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("TORCH_HOME", str((cache_root / "torch").resolve()))
    env.setdefault("HF_HOME", str((cache_root / "huggingface").resolve()))
    env.setdefault("TRANSFORMERS_CACHE", str((cache_root / "huggingface").resolve()))
    env.setdefault("XDG_CACHE_HOME", str((cache_root / "xdg").resolve()))
    env.setdefault("PIP_CACHE_DIR", str((cache_root / "pip").resolve()))
    env.setdefault("HOME", str((cache_root / "home").resolve()))
    env.setdefault("TMPDIR", str((cache_root / "tmp").resolve()))
    env.setdefault("NCCL_ASYNC_ERROR_HANDLING", "1")
    return env


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_command_and_write_results(
    *,
    stage: str,
    task: str,
    framework: str,
    timeout_sec: int,
    report_path: Path,
    cli_python: Optional[str],
    requires_python: bool,
    env_overrides: List[str],
    decision_reason: str,
    failure_category_on_failure: str,
    out_dir: Path,
    command_tokens: List[str],
) -> int:
    repo_root = _repo_root()
    out_dir = out_dir.resolve()
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    base_env = _runner_env(repo_root, os.environ)
    for item in env_overrides:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        base_env[k] = v

    started_utc = _utc_timestamp()

    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": task,
        "command": "",
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": _default_assets(repo_root),
        "meta": {
            "python": "",
            "python_source": "",
            "git_commit": _git_commit(repo_root),
            "env_vars": {},
            "decision_reason": decision_reason,
            "timestamp_utc": started_utc,
            "warnings": [],
            "command_returncode": None,
            "ended_utc": "",
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    try:
        needs_py = requires_python or "{python}" in command_tokens
        resolved = resolve_python(
            cli_python=cli_python, report_path=report_path, requires_python=needs_py
        )
        results["meta"]["python"] = resolved.python
        results["meta"]["python_source"] = resolved.source
        results["meta"]["warnings"].extend(resolved.warnings)
        command_tokens = _apply_python_placeholder(command_tokens, resolved.python)
    except MissingReportError as e:
        _ensure_dir(out_dir)
        log_path.write_text(
            f"[runner] stage={stage} failure: missing report\n{e}\n", encoding="utf-8"
        )
        results["command"] = _cmd_to_str(command_tokens) if command_tokens else ""
        results["failure_category"] = "missing_report"
        results["error_excerpt"] = _read_text_tail(log_path)
        results["meta"]["ended_utc"] = _utc_timestamp()
        results["meta"]["env_vars"] = _select_env_vars(base_env)
        _write_json(results_path, results)
        return 1
    except InvalidPythonPathError as e:
        _ensure_dir(out_dir)
        log_path.write_text(
            f"[runner] stage={stage} failure: invalid python path\n{e}\n",
            encoding="utf-8",
        )
        results["command"] = _cmd_to_str(command_tokens) if command_tokens else ""
        results["failure_category"] = "path_hallucination"
        results["error_excerpt"] = _read_text_tail(log_path)
        results["meta"]["ended_utc"] = _utc_timestamp()
        results["meta"]["env_vars"] = _select_env_vars(base_env)
        _write_json(results_path, results)
        return 1
    except Exception as e:
        _ensure_dir(out_dir)
        log_path.write_text(
            f"[runner] stage={stage} failure: unexpected error\n{e}\n",
            encoding="utf-8",
        )
        results["command"] = _cmd_to_str(command_tokens) if command_tokens else ""
        results["failure_category"] = "unknown"
        results["error_excerpt"] = _read_text_tail(log_path)
        results["meta"]["ended_utc"] = _utc_timestamp()
        results["meta"]["env_vars"] = _select_env_vars(base_env)
        _write_json(results_path, results)
        return 1

    command_tokens = _normalize_command_tokens(command_tokens)
    cmd_str = _cmd_to_str(command_tokens)
    results["command"] = cmd_str
    results["meta"]["env_vars"] = _select_env_vars(base_env)

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[runner] repo_root={repo_root}\n")
        log.write(f"[runner] stage={stage} task={task} framework={framework}\n")
        log.write(f"[runner] started_utc={started_utc}\n")
        if results["meta"]["python"]:
            log.write(
                f"[runner] resolved_python={results['meta']['python']} "
                f"(source={results['meta']['python_source']})\n"
            )
        for w in results["meta"]["warnings"]:
            log.write(f"[runner] warning: {w}\n")
        log.write(f"[runner] timeout_sec={timeout_sec}\n")
        log.write(f"[runner] command={cmd_str}\n\n")
        log.flush()

        if not command_tokens:
            log.write("[runner] error: empty command\n")
            results["failure_category"] = "entrypoint_not_found"
            results["error_excerpt"] = _read_text_tail(log_path)
            results["meta"]["ended_utc"] = _utc_timestamp()
            _write_json(results_path, results)
            return 1

        try:
            proc = subprocess.Popen(
                command_tokens,
                cwd=str(repo_root),
                env=base_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                proc.wait(timeout=timeout_sec)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=30)
                except Exception:
                    pass
                rc = 124
                log.write(f"\n[runner] timeout after {timeout_sec}s\n")
        except FileNotFoundError as e:
            results["failure_category"] = "entrypoint_not_found"
            log.write(f"\n[runner] FileNotFoundError: {e}\n")
            results["meta"]["command_returncode"] = None
            results["error_excerpt"] = _read_text_tail(log_path)
            results["meta"]["ended_utc"] = _utc_timestamp()
            _write_json(results_path, results)
            return 1
        except Exception as e:
            results["failure_category"] = "runtime"
            log.write(f"\n[runner] Exception: {e}\n")
            results["meta"]["command_returncode"] = None
            results["error_excerpt"] = _read_text_tail(log_path)
            results["meta"]["ended_utc"] = _utc_timestamp()
            _write_json(results_path, results)
            return 1

    results["meta"]["command_returncode"] = int(rc) if rc is not None else None
    results["meta"]["ended_utc"] = _utc_timestamp()

    if rc == 0:
        results["status"] = "success"
        results["exit_code"] = 0
        results["failure_category"] = "unknown"
        results["error_excerpt"] = ""
        _write_json(results_path, results)
        return 0

    results["status"] = "failure"
    results["exit_code"] = 1
    if rc == 124:
        results["failure_category"] = "timeout"
    else:
        results["failure_category"] = failure_category_on_failure or "runtime"
    results["error_excerpt"] = _read_text_tail(log_path)
    _write_json(results_path, results)
    return 1


def _parse_env_overrides(values: Optional[List[str]]) -> List[str]:
    if not values:
        return []
    out: List[str] = []
    for v in values:
        out.append(v)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark runner.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run a command and emit build_output/<stage> results.")
    p_run.add_argument("--stage", required=True)
    p_run.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    p_run.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    p_run.add_argument("--timeout-sec", type=int, default=0)
    p_run.add_argument("--out-dir", default="")
    p_run.add_argument("--report-path", default="")
    p_run.add_argument("--python", default="")
    p_run.add_argument("--requires-python", action="store_true")
    p_run.add_argument("--env", action="append", default=[], help="Repeatable KEY=VAL env override")
    p_run.add_argument("--decision-reason", default="")
    p_run.add_argument("--failure-category", default="runtime")
    p_run.add_argument("command", nargs=argparse.REMAINDER)

    p_resolve = sub.add_parser("resolve-python", help="Print resolved python interpreter path.")
    p_resolve.add_argument("--report-path", default="")
    p_resolve.add_argument("--python", default="")
    p_resolve.add_argument("--requires-python", action="store_true")

    args = parser.parse_args(argv)
    repo_root = _repo_root()

    if args.cmd == "resolve-python":
        report_path = _resolve_report_path(args.report_path)
        try:
            resolved = resolve_python(
                cli_python=args.python or None,
                report_path=report_path,
                requires_python=args.requires_python,
            )
        except Exception as e:
            print(str(e), file=sys.stderr)
            return 1
        print(resolved.python)
        return 0

    if args.cmd == "run":
        stage = args.stage
        out_dir = Path(args.out_dir) if args.out_dir else (repo_root / "build_output" / stage)
        timeout = int(args.timeout_sec) if args.timeout_sec and args.timeout_sec > 0 else _stage_defaults(stage)
        report_path = _resolve_report_path(args.report_path)
        cmd_tokens = args.command
        if cmd_tokens and cmd_tokens[0] == "--":
            cmd_tokens = cmd_tokens[1:]
        env_overrides = _parse_env_overrides(args.env)
        return run_command_and_write_results(
            stage=stage,
            task=args.task,
            framework=args.framework,
            timeout_sec=timeout,
            report_path=report_path,
            cli_python=args.python or None,
            requires_python=bool(args.requires_python),
            env_overrides=env_overrides,
            decision_reason=args.decision_reason,
            failure_category_on_failure=args.failure_category,
            out_dir=out_dir,
            command_tokens=cmd_tokens,
        )

    raise RuntimeError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())

