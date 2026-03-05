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


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def _safe_env_subset(env: Dict[str, str]) -> Dict[str, str]:
    keep_prefixes = ("SCIMLOPSBENCH_", "CUDA_", "HF_", "TRANSFORMERS_", "TORCH", "PYTHON")
    keep_keys = {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "PWD",
        "VIRTUAL_ENV",
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
    }
    out: Dict[str, str] = {}
    for k, v in env.items():
        if k in keep_keys or any(k.startswith(p) for p in keep_prefixes):
            out[k] = v
    return out


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = text.splitlines()
    tail = lines[-max_lines:]
    return "\n".join(tail)


def _infer_failure_category_from_excerpt(excerpt: str) -> str:
    text = excerpt.lower()

    deps_markers = (
        "modulenotfounderror",
        "no module named",
        "cannot open shared object file",
        "undefined symbol",
        "dll load failed",
        "could not load any common_ops library",
        "[sgl_kernel] critical",
    )
    if any(m in text for m in deps_markers):
        return "deps"

    oom_markers = (
        "cuda out of memory",
        "cublas_status_alloc_failed",
        "outofmemoryerror",
        "hip out of memory",
    )
    if any(m in text for m in oom_markers):
        return "oom"

    args_markers = (
        "unrecognized arguments",
        "unknown argument",
        "error: the following arguments are required",
    )
    if any(m in text for m in args_markers):
        return "args_unknown"

    auth_markers = (
        "401 client error",
        "403 client error",
        "requires authentication",
        "hf_auth_token",
        "hugging face token",
        "gated repo",
    )
    if any(m in text for m in auth_markers):
        return "auth_required"

    download_markers = (
        "name or service not known",
        "temporary failure in name resolution",
        "connection refused",
        "connection error",
        "read timed out",
        "max retries exceeded",
    )
    if any(m in text for m in download_markers):
        return "download_failed"

    return "runtime"


def _git_commit(repo_root: Path) -> str:
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return ""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()
        return out
    except Exception:
        return ""


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def _load_report(report_path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = report_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception:
        return None, "missing_report"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(data, dict):
        return None, "invalid_json"
    return data, None


def _is_executable_file(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(str(path), os.X_OK)


def _resolve_python(
    *,
    cli_python: Optional[str],
    requires_python: bool,
    report_path: Path,
) -> Tuple[Optional[str], Dict[str, Any]]:
    meta: Dict[str, Any] = {
        "resolved_from": None,
        "warnings": [],
        "report_path": str(report_path),
        "report_loaded": False,
    }

    if cli_python:
        meta["resolved_from"] = "cli"
        return cli_python, meta

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        meta["resolved_from"] = "env"
        return env_python, meta

    report, report_err = _load_report(report_path)
    if report is None:
        meta["report_loaded"] = False
        meta["report_error"] = report_err
        if requires_python:
            return None, meta
        # For non-Python stages, allow a best-effort fallback.
        fallback = shutil.which("python3") or shutil.which("python") or None
        if fallback:
            meta["resolved_from"] = "path_fallback"
            meta["warnings"].append("report_missing_or_invalid_used_path_fallback")
        return fallback, meta

    meta["report_loaded"] = True
    python_path = report.get("python_path")
    if isinstance(python_path, str) and python_path.strip():
        meta["resolved_from"] = "report"
        return python_path, meta

    # Report exists but doesn't include python_path.
    meta["resolved_from"] = "path_fallback"
    meta["warnings"].append("report_missing_python_path_used_path_fallback")
    fallback = shutil.which("python3") or shutil.which("python") or None
    return fallback, meta


def _default_timeout(stage: str) -> int:
    return {
        "prepare": 1200,
        "cpu": 600,
        "single_gpu": 600,
        "multi_gpu": 1200,
        "env_size": 120,
        "hallucination": 120,
        "cuda": 120,
        "pyright": 600,
        "summary": 120,
    }.get(stage, 600)


def _command_to_str(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _parse_env_overrides(items: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--env expects KEY=VALUE, got: {item!r}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def _base_results(
    *,
    stage: str,
    task: str,
    command: str,
    timeout_sec: int,
    framework: str,
    python_exe: Optional[str],
    python_meta: Dict[str, Any],
    decision_reason: str,
    env_vars: Dict[str, str],
    dataset: Dict[str, str],
    model: Dict[str, str],
    repo_root: Path,
) -> Dict[str, Any]:
    return {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": task,
        "command": command,
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": {
            "dataset": dataset,
            "model": model,
        },
        "meta": {
            "python": python_exe or "",
            "python_resolution": python_meta,
            "git_commit": _git_commit(repo_root),
            "env_vars": env_vars,
            "decision_reason": decision_reason,
            "timestamp_utc": _utc_now_iso(),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark stage runner")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    parser.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--out-root", default="build_output")
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python", dest="python_bin", default=None)
    parser.add_argument("--requires-python", dest="requires_python", action="store_true", default=True)
    parser.add_argument("--no-requires-python", dest="requires_python", action="store_false")
    parser.add_argument("--allow-nonzero", action="store_true")
    parser.add_argument("--skip", action="store_true")
    parser.add_argument("--skip-reason", default="unknown", choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"])
    parser.add_argument("--failure-category", default=None)
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--env", action="append", default=[], help="Repeatable KEY=VALUE env overrides for the executed command")
    parser.add_argument("--dataset-path", default="")
    parser.add_argument("--dataset-source", default="")
    parser.add_argument("--dataset-version", default="")
    parser.add_argument("--dataset-sha256", default="")
    parser.add_argument("--model-path", default="")
    parser.add_argument("--model-source", default="")
    parser.add_argument("--model-version", default="")
    parser.add_argument("--model-sha256", default="")
    parser.add_argument("--shell-cmd", default=None, help="Run a single shell command via bash -lc")
    parser.add_argument("--print-resolved-python", action="store_true", help="Print resolved python path and exit")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run (prefix with --)")

    args = parser.parse_args(argv)

    repo_root = _repo_root()
    stage = args.stage
    out_dir = repo_root / args.out_root / stage
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = args.timeout_sec if args.timeout_sec is not None else _default_timeout(stage)

    report_path = _resolve_report_path(args.report_path)
    python_exe, python_meta = _resolve_python(
        cli_python=args.python_bin,
        requires_python=args.requires_python,
        report_path=report_path,
    )

    if args.print_resolved_python:
        if python_exe:
            print(python_exe)
            return 0
        return 1

    env_overrides = _parse_env_overrides(args.env)
    env_for_cmd = os.environ.copy()
    env_for_cmd.update(env_overrides)
    safe_env = _safe_env_subset(env_for_cmd)

    dataset = {
        "path": args.dataset_path,
        "source": args.dataset_source,
        "version": args.dataset_version,
        "sha256": args.dataset_sha256,
    }
    model = {
        "path": args.model_path,
        "source": args.model_source,
        "version": args.model_version,
        "sha256": args.model_sha256,
    }

    # Determine the command to run.
    cmd: Optional[List[str]] = None
    cmd_str: str = ""
    if args.shell_cmd:
        cmd = ["bash", "-lc", args.shell_cmd]
        cmd_str = args.shell_cmd
    else:
        cmd = [c for c in args.cmd if c != "--"]
        if cmd:
            cmd_str = _command_to_str(cmd)

    results = _base_results(
        stage=stage,
        task=args.task,
        command=cmd_str,
        timeout_sec=timeout_sec,
        framework=args.framework,
        python_exe=python_exe,
        python_meta=python_meta,
        decision_reason=args.decision_reason,
        env_vars=safe_env,
        dataset=dataset,
        model=model,
        repo_root=repo_root,
    )

    def finalize_and_write(exit_code: int, status: str, failure_category: str, skip_reason: str = "unknown") -> int:
        results["exit_code"] = int(exit_code)
        results["status"] = status
        results["skip_reason"] = skip_reason
        results["failure_category"] = failure_category
        results["error_excerpt"] = _tail_lines(log_path, max_lines=220)
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return exit_code

    try:
        with log_path.open("w", encoding="utf-8") as log_f:
            log_f.write(f"[runner] timestamp_utc={_utc_now_iso()}\n")
            log_f.write(f"[runner] repo_root={repo_root}\n")
            log_f.write(f"[runner] stage={stage} task={args.task} framework={args.framework}\n")
            log_f.write(f"[runner] timeout_sec={timeout_sec}\n")
            log_f.write(f"[runner] resolved_python={python_exe or ''}\n")
            if python_meta.get("warnings"):
                log_f.write(f"[runner] python_resolution_warnings={python_meta['warnings']}\n")
            if env_overrides:
                log_f.write(f"[runner] env_overrides={env_overrides}\n")

            if args.skip:
                log_f.write(f"[runner] skipped skip_reason={args.skip_reason}\n")
                log_f.flush()
                if not results.get("command"):
                    results["command"] = "<skipped>"
                return finalize_and_write(0, "skipped", "unknown", skip_reason=args.skip_reason)

            if args.requires_python and python_exe is None:
                log_f.write("[runner] ERROR: missing/invalid report and no --python provided.\n")
                log_f.flush()
                return finalize_and_write(1, "failure", "missing_report")

            if not cmd:
                log_f.write("[runner] ERROR: no command provided.\n")
                log_f.flush()
                return finalize_and_write(1, "failure", "entrypoint_not_found")

            log_f.write(f"[runner] command={cmd_str}\n")
            log_f.flush()

            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(repo_root),
                    env=env_for_cmd,
                    stdout=log_f,
                    stderr=log_f,
                    text=True,
                    timeout=timeout_sec,
                )
                results["meta"]["command_exit_code"] = proc.returncode
            except subprocess.TimeoutExpired:
                log_f.write("[runner] ERROR: timeout\n")
                log_f.flush()
                results["meta"]["command_exit_code"] = None
                return finalize_and_write(1, "failure", "timeout")
            except FileNotFoundError:
                log_f.write("[runner] ERROR: command not found\n")
                log_f.flush()
                results["meta"]["command_exit_code"] = None
                return finalize_and_write(1, "failure", "entrypoint_not_found")

            if proc.returncode == 0 or args.allow_nonzero:
                return finalize_and_write(0, "success", "unknown")

            # Non-zero exit
            failure_category = args.failure_category
            if failure_category is None:
                failure_category = _infer_failure_category_from_excerpt(_tail_lines(log_path, max_lines=220))
            return finalize_and_write(1, "failure", failure_category)
    except Exception:
        # Best-effort failure reporting
        try:
            with log_path.open("a", encoding="utf-8") as log_f:
                log_f.write("[runner] INTERNAL ERROR\n")
                log_f.write(traceback.format_exc())
        except Exception:
            pass
        results["failure_category"] = "unknown"
        results["error_excerpt"] = _tail_lines(log_path, max_lines=220)
        try:
            results_path.write_text(
                json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
