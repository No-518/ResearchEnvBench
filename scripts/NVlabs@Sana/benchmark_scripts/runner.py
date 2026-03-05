#!/usr/bin/env python3
"""
Unified command runner for benchmark stages.

This script is intentionally stdlib-only so it can run from a "system" python,
while executing benchmark commands using the python interpreter declared in the
agent report (unless overridden).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"

DEFAULT_STAGE_TIMEOUTS_SEC: Dict[str, int] = {
    "pyright": 600,
    "prepare": 1200,
    "cpu": 600,
    "cuda": 120,
    "single_gpu": 600,
    "multi_gpu": 1200,
    "env_size": 120,
    "hallucination": 120,
    "summary": 120,
}


class RunnerError(RuntimeError):
    pass


class MissingReportError(RunnerError):
    pass


class InvalidReportError(RunnerError):
    pass


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(_read_text(path))


def _write_json_atomic(path: Path, payload: Any) -> None:
    _safe_mkdir(path.parent)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as f:
        tmp = Path(f.name)
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def _git_commit(repo_root: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        return ""
    return ""


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def _load_report(report_path: Path) -> Dict[str, Any]:
    if not report_path.exists():
        raise MissingReportError(f"report.json not found at {report_path}")
    try:
        data = _read_json(report_path)
    except Exception as e:
        raise InvalidReportError(f"failed to parse report.json at {report_path}: {e}") from e
    if not isinstance(data, dict):
        raise InvalidReportError("report.json must be a JSON object")
    return data


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


@dataclass(frozen=True)
class ResolvedPython:
    python: str
    warning: str = ""
    report_path: str = ""
    reported_python_path: str = ""


def resolve_python_interpreter(
    *,
    cli_python: Optional[str],
    require_report: bool,
    cli_report_path: Optional[str],
) -> ResolvedPython:
    report_path = _resolve_report_path(cli_report_path)

    if cli_python:
        python = cli_python
        p = Path(python)
        if not _is_executable_file(p):
            raise RunnerError(f"--python is not an executable file: {python}")
        return ResolvedPython(python=python, report_path=str(report_path))

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        p = Path(env_python)
        if not _is_executable_file(p):
            raise RunnerError(f"SCIMLOPSBENCH_PYTHON is not an executable file: {env_python}")
        return ResolvedPython(python=env_python, report_path=str(report_path))

    reported_python_path = ""
    try:
        report = _load_report(report_path)
        reported_python_path = str(report.get("python_path", "") or "")
        if not reported_python_path:
            raise InvalidReportError("report.json missing required field: python_path")
        p = Path(reported_python_path)
        if not _is_executable_file(p):
            raise RunnerError(f"python_path from report.json is not an executable file: {reported_python_path}")
        return ResolvedPython(
            python=reported_python_path,
            report_path=str(report_path),
            reported_python_path=reported_python_path,
        )
    except (MissingReportError, InvalidReportError, RunnerError):
        if require_report:
            raise

    fallback = shutil.which("python3") or shutil.which("python") or ""
    if not fallback:
        raise RunnerError("could not resolve a python interpreter (no report/override, and no python in PATH)")
    warning = "fallback_to_path_python"
    return ResolvedPython(
        python=fallback,
        warning=warning,
        report_path=str(report_path),
        reported_python_path=reported_python_path,
    )


def _load_prepare_assets(repo_root: Path) -> Dict[str, Any]:
    prepare_results = repo_root / "build_output" / "prepare" / "results.json"
    if not prepare_results.exists():
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    try:
        data = _read_json(prepare_results)
        assets = data.get("assets") if isinstance(data, dict) else None
        if isinstance(assets, dict) and isinstance(assets.get("dataset"), dict) and isinstance(assets.get("model"), dict):
            return {
                "dataset": {
                    "path": str(assets["dataset"].get("path", "") or ""),
                    "source": str(assets["dataset"].get("source", "") or ""),
                    "version": str(assets["dataset"].get("version", "") or ""),
                    "sha256": str(assets["dataset"].get("sha256", "") or ""),
                },
                "model": {
                    "path": str(assets["model"].get("path", "") or ""),
                    "source": str(assets["model"].get("source", "") or ""),
                    "version": str(assets["model"].get("version", "") or ""),
                    "sha256": str(assets["model"].get("sha256", "") or ""),
                },
            }
    except Exception:
        pass
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _env_snapshot(keys: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k in keys:
        if k in os.environ:
            out[k] = os.environ.get(k, "")
    return out


def _python_version(python: str) -> str:
    try:
        r = subprocess.run(
            [python, "-c", "import platform; print(platform.python_version())"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        return ""
    return ""


def _render_command(template: str, python: str) -> str:
    return template.replace("{python}", shlex.quote(python))


def _parse_success_exit_codes(csv: str) -> List[int]:
    codes: List[int] = []
    for part in csv.split(","):
        part = part.strip()
        if not part:
            continue
        codes.append(int(part))
    if not codes:
        return [0]
    return codes


def run_stage(
    *,
    stage: str,
    task: str,
    framework: str,
    command_template: str,
    timeout_sec: int,
    requires_python: bool,
    cli_python: Optional[str],
    cli_report_path: Optional[str],
    success_exit_codes: List[int],
    skip_reason: str,
    skip: bool,
    decision_reason: str,
    meta_json_path: Optional[str],
) -> int:
    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / stage
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    # Start log early so we always have a file even on runner errors.
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[runner] stage={stage} task={task}\n")
        log.write(f"[runner] repo_root={repo_root}\n")
        log.flush()

        python_resolved: Optional[ResolvedPython] = None
        python_warning = ""
        try:
            python_resolved = resolve_python_interpreter(
                cli_python=cli_python, require_report=requires_python, cli_report_path=cli_report_path
            )
            python_warning = python_resolved.warning
        except MissingReportError as e:
            log.write(f"[runner] missing_report: {e}\n")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": stage,
                "task": task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": framework,
                "assets": _load_prepare_assets(repo_root),
                "meta": {
                    "python": "",
                    "git_commit": _git_commit(repo_root),
                    "env_vars": _env_snapshot(
                        [
                            "CUDA_VISIBLE_DEVICES",
                            "HF_HOME",
                            "HUGGINGFACE_HUB_CACHE",
                            "TRANSFORMERS_CACHE",
                            "TORCH_HOME",
                            "HOME",
                            "XDG_CACHE_HOME",
                            "TMPDIR",
                        ]
                    ),
                    "decision_reason": decision_reason,
                    "report_path": str(_resolve_report_path(cli_report_path)),
                },
                "failure_category": "missing_report",
                "error_excerpt": _tail_lines(log_path),
            }
            _write_json_atomic(results_path, payload)
            return 1
        except InvalidReportError as e:
            log.write(f"[runner] invalid_report: {e}\n")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": stage,
                "task": task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": framework,
                "assets": _load_prepare_assets(repo_root),
                "meta": {
                    "python": "",
                    "git_commit": _git_commit(repo_root),
                    "env_vars": _env_snapshot(
                        [
                            "CUDA_VISIBLE_DEVICES",
                            "HF_HOME",
                            "HUGGINGFACE_HUB_CACHE",
                            "TRANSFORMERS_CACHE",
                            "TORCH_HOME",
                            "HOME",
                            "XDG_CACHE_HOME",
                            "TMPDIR",
                        ]
                    ),
                    "decision_reason": decision_reason,
                    "report_path": str(_resolve_report_path(cli_report_path)),
                },
                "failure_category": "invalid_json",
                "error_excerpt": _tail_lines(log_path),
            }
            _write_json_atomic(results_path, payload)
            return 1
        except RunnerError as e:
            log.write(f"[runner] runner_error: {e}\n")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": stage,
                "task": task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": framework,
                "assets": _load_prepare_assets(repo_root),
                "meta": {
                    "python": "",
                    "git_commit": _git_commit(repo_root),
                    "env_vars": _env_snapshot(
                        [
                            "CUDA_VISIBLE_DEVICES",
                            "HF_HOME",
                            "HUGGINGFACE_HUB_CACHE",
                            "TRANSFORMERS_CACHE",
                            "TORCH_HOME",
                            "HOME",
                            "XDG_CACHE_HOME",
                            "TMPDIR",
                        ]
                    ),
                    "decision_reason": decision_reason,
                    "report_path": str(_resolve_report_path(cli_report_path)),
                },
                "failure_category": "path_hallucination",
                "error_excerpt": _tail_lines(log_path),
            }
            _write_json_atomic(results_path, payload)
            return 1

        assert python_resolved is not None

        if skip:
            log.write(f"[runner] skipped: {skip_reason}\n")
            payload = {
                "status": "skipped",
                "skip_reason": skip_reason,
                "exit_code": 0,
                "stage": stage,
                "task": task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": framework,
                "assets": _load_prepare_assets(repo_root),
                "meta": {
                    "python": _python_version(python_resolved.python),
                    "git_commit": _git_commit(repo_root),
                    "env_vars": _env_snapshot(
                        [
                            "CUDA_VISIBLE_DEVICES",
                            "HF_HOME",
                            "HUGGINGFACE_HUB_CACHE",
                            "TRANSFORMERS_CACHE",
                            "TORCH_HOME",
                            "HOME",
                            "XDG_CACHE_HOME",
                            "TMPDIR",
                        ]
                    ),
                    "decision_reason": decision_reason,
                    "python_path": python_resolved.python,
                    "python_resolution_warning": python_warning,
                    "report_path": python_resolved.report_path,
                },
                "failure_category": "",
                "error_excerpt": "",
            }
            _write_json_atomic(results_path, payload)
            return 0

        command = _render_command(command_template, python_resolved.python)
        log.write(f"[runner] python={python_resolved.python}\n")
        if python_warning:
            log.write(f"[runner] python_warning={python_warning}\n")
        log.write(f"[runner] command={command}\n")
        log.flush()

        start = time.time()
        raw_exit = 1
        failure_category = ""
        status = "failure"
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(repo_root),
                env=os.environ.copy(),
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout_sec,
                executable="/bin/bash",
                check=False,
            )
            raw_exit = int(proc.returncode)
            if raw_exit in success_exit_codes:
                status = "success"
                failure_category = ""
            else:
                status = "failure"
                failure_category = "runtime"
        except subprocess.TimeoutExpired:
            raw_exit = 124
            status = "failure"
            failure_category = "timeout"
            log.write("[runner] timeout\n")
        except Exception as e:
            raw_exit = 1
            status = "failure"
            failure_category = "unknown"
            log.write(f"[runner] exception: {e}\n")
        duration = time.time() - start

    # Merge optional meta json after command to avoid affecting execution.
    extra_meta: Dict[str, Any] = {}
    if meta_json_path:
        try:
            extra_meta_data = _read_json(Path(meta_json_path))
            if isinstance(extra_meta_data, dict):
                extra_meta = extra_meta_data
        except Exception:
            extra_meta = {"meta_json_error": f"failed_to_read:{meta_json_path}"}

    exit_code = 0 if status in ("success", "skipped") else 1
    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": stage,
        "task": task,
        "command": command,
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": _load_prepare_assets(_repo_root()),
        "meta": {
            "python": _python_version(python_resolved.python) if requires_python else "",
            "git_commit": _git_commit(_repo_root()),
            "env_vars": _env_snapshot(
                [
                    "CUDA_VISIBLE_DEVICES",
                    "HF_HOME",
                    "HUGGINGFACE_HUB_CACHE",
                    "TRANSFORMERS_CACHE",
                    "TORCH_HOME",
                    "HOME",
                    "XDG_CACHE_HOME",
                    "TMPDIR",
                ]
            ),
            "decision_reason": decision_reason,
            "python_path": python_resolved.python if requires_python else "",
            "python_resolution_warning": python_warning,
            "report_path": python_resolved.report_path,
            "command_exit_code": raw_exit,
            "duration_sec": round(duration, 3),
            **extra_meta,
        },
        "failure_category": failure_category if status == "failure" else "",
        "error_excerpt": _tail_lines(_repo_root() / "build_output" / stage / "log.txt"),
    }
    _write_json_atomic(_repo_root() / "build_output" / stage / "results.json", payload)
    return exit_code


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runner.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("resolve-python", help="Print resolved python interpreter path.")
    rp.add_argument("--python", dest="python", default=None)
    rp.add_argument("--report-path", dest="report_path", default=None)
    rp.add_argument("--require-report", action="store_true")

    run = sub.add_parser("run", help="Run a stage command and write build_output/<stage>/{log.txt,results.json}.")
    run.add_argument("--stage", required=True)
    run.add_argument("--task", required=True)
    run.add_argument("--framework", default="unknown")
    run.add_argument("--command", required=True, help="Shell command template (supports {python} placeholder).")
    run.add_argument("--timeout-sec", type=int, default=0)
    run.add_argument("--requires-python", action="store_true")
    run.add_argument("--python", dest="python", default=None)
    run.add_argument("--report-path", dest="report_path", default=None)
    run.add_argument("--success-exit-codes", default="0")
    run.add_argument("--skip", action="store_true")
    run.add_argument("--skip-reason", default="unknown")
    run.add_argument("--decision-reason", default="")
    run.add_argument("--meta-json", default=None)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "resolve-python":
        try:
            resolved = resolve_python_interpreter(
                cli_python=args.python, require_report=args.require_report, cli_report_path=args.report_path
            )
        except Exception as e:
            print(str(e), file=sys.stderr)
            return 1
        print(resolved.python)
        return 0

    if args.cmd == "run":
        timeout = args.timeout_sec or DEFAULT_STAGE_TIMEOUTS_SEC.get(args.stage, 600)
        codes = _parse_success_exit_codes(args.success_exit_codes)
        return run_stage(
            stage=args.stage,
            task=args.task,
            framework=args.framework,
            command_template=args.command,
            timeout_sec=timeout,
            requires_python=bool(args.requires_python),
            cli_python=args.python,
            cli_report_path=args.report_path,
            success_exit_codes=codes,
            skip_reason=args.skip_reason,
            skip=bool(args.skip),
            decision_reason=args.decision_reason,
            meta_json_path=args.meta_json,
        )

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
