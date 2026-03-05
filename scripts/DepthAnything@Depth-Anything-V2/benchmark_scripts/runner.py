#!/usr/bin/env python3
import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_env_snapshot() -> Dict[str, str]:
    allow_keys = {
        "CUDA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "HF_HOME",
        "HF_DATASETS_CACHE",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "PYTHONPATH",
        "PATH",
    }
    redaction_markers = ("TOKEN", "KEY", "SECRET", "PASSWORD")
    out: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key in allow_keys or key.startswith("SCIMLOPSBENCH_"):
            if any(marker in key.upper() for marker in redaction_markers):
                out[key] = "<redacted>"
            else:
                out[key] = value
    return out


def _read_text_tail(path: Path, max_lines: int = 220) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    tail = data[-max_lines:]
    return "\n".join(tail)


def _try_git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def resolve_report_path(cli_report_path: Optional[str]) -> str:
    if cli_report_path:
        return cli_report_path
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return env_path
    return DEFAULT_REPORT_PATH


def _load_report(report_path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    p = Path(report_path)
    if not p.exists():
        return None, f"report_not_found: {report_path}"
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, f"report_invalid_json: {report_path}: {e}"


@dataclass
class PythonResolution:
    python_executable: str
    source: str
    warnings: List[str]


def resolve_python(
    *,
    cli_python: Optional[str],
    report_path: str,
    requires_python: bool,
) -> Tuple[Optional[PythonResolution], Optional[str]]:
    """
    Resolution priority:
      1) CLI --python
      2) env SCIMLOPSBENCH_PYTHON
      3) python_path from report.json
      4) fallback python from PATH (only if report exists but python_path missing/invalid)
    If report is missing/invalid and no CLI/env override is provided, fail with missing_report
    (except when requires_python is False).
    """
    warnings: List[str] = []

    if cli_python:
        return PythonResolution(cli_python, "cli", warnings), None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return PythonResolution(env_python, "env:SCIMLOPSBENCH_PYTHON", warnings), None

    report, report_err = _load_report(report_path)
    if report is None:
        if requires_python:
            return None, report_err or f"report_unavailable: {report_path}"
        return PythonResolution("python", "path_fallback_no_report", ["report_unavailable_but_not_required"]), None

    python_path = report.get("python_path")
    if isinstance(python_path, str) and python_path.strip():
        candidate = python_path.strip()
        if Path(candidate).exists() and os.access(candidate, os.X_OK):
            return PythonResolution(candidate, "report:python_path", warnings), None
        warnings.append(f"report_python_path_not_executable: {candidate}")
        warnings.append("falling_back_to_python_in_PATH")
        return PythonResolution("python", "path_fallback", warnings), None

    if requires_python:
        warnings.append("report_missing_python_path")
        warnings.append("falling_back_to_python_in_PATH")
        return PythonResolution("python", "path_fallback", warnings), None

    return PythonResolution("python", "path_fallback", warnings), None


def _python_version(python_executable: str) -> str:
    try:
        cp = subprocess.run(
            [python_executable, "-c", "import platform; print(platform.python_version())"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def _default_timeout_for_stage(stage: str) -> int:
    stage = stage.strip()
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
    return int(defaults.get(stage, 600))


def _classify_failure(log_tail: str, timed_out: bool, program_not_found: bool) -> str:
    if program_not_found:
        return "entrypoint_not_found"
    if timed_out:
        return "timeout"
    lower = log_tail.lower()
    if "unrecognized arguments" in lower or "unknown argument" in lower or "no such option" in lower:
        return "args_unknown"
    if "cuda out of memory" in lower or "out of memory" in lower:
        return "oom"
    if "no module named" in lower or "modulenotfounderror" in lower:
        return "deps"
    return "runtime"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Unified benchmark command runner.")
    ap.add_argument("--stage", required=True)
    ap.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    ap.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--timeout-sec", type=int, default=0)
    ap.add_argument("--python", dest="cli_python", default=None)
    ap.add_argument("--report-path", default=None)
    ap.add_argument("--requires-python", action="store_true")
    ap.add_argument("--assets-from", default=None, help="Path to JSON containing an `assets` object to copy.")
    ap.add_argument("--decision-reason", default="")
    ap.add_argument("--skip", action="store_true")
    ap.add_argument(
        "--skip-reason",
        default="unknown",
        choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"],
    )

    mode = ap.add_mutually_exclusive_group(required=False)
    mode.add_argument("--python-script", default=None, help="Run a python script with the resolved interpreter.")
    mode.add_argument("--python-module", default=None, help="Run `python -m <module>` with the resolved interpreter.")

    ap.add_argument("--", dest="cmd_sep", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("command", nargs=argparse.REMAINDER, help="Command to execute (after --).")

    args = ap.parse_args()

    # argparse.REMAINDER includes the literal "--" sentinel; strip it so it does not get passed
    # through to repository entrypoints (which may treat it as "end of options" and reject args).
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]

    repo_root = _repo_root()
    out_dir = Path(args.out_dir)
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)

    timeout_sec = int(args.timeout_sec) if args.timeout_sec and args.timeout_sec > 0 else _default_timeout_for_stage(args.stage)

    stage_exit_code = 0
    status = "success"
    failure_category = "unknown"
    skip_reason = "unknown"
    command_str = ""
    command_returncode: Optional[int] = None
    timed_out = False
    program_not_found = False

    assets: Dict[str, Any] = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    if args.assets_from:
        try:
            assets_from = json.loads(Path(args.assets_from).read_text(encoding="utf-8"))
            if isinstance(assets_from, dict) and isinstance(assets_from.get("assets"), dict):
                assets = assets_from["assets"]
        except Exception:
            pass

    requires_python = bool(args.requires_python or args.python_script or args.python_module)

    py_res: Optional[PythonResolution] = None
    py_err: Optional[str] = None
    if requires_python or args.skip:
        py_res, py_err = resolve_python(cli_python=args.cli_python, report_path=report_path, requires_python=requires_python)
        if py_res is None and requires_python:
            status = "failure"
            stage_exit_code = 1
            failure_category = "missing_report"
            skip_reason = "unknown"
            with log_path.open("w", encoding="utf-8") as f:
                f.write(f"[runner] Failed to resolve python. report_path={report_path}\n")
                f.write(f"[runner] error={py_err}\n")
            payload = {
                "status": status,
                "skip_reason": skip_reason,
                "exit_code": stage_exit_code,
                "stage": args.stage,
                "task": args.task,
                "command": command_str,
                "timeout_sec": timeout_sec,
                "framework": args.framework,
                "assets": assets,
                "meta": {
                    "python": "",
                    "git_commit": _try_git_commit(repo_root),
                    "env_vars": _safe_env_snapshot(),
                    "decision_reason": args.decision_reason,
                    "timestamp_utc": _utc_now_iso(),
                    "warnings": [],
                },
                "failure_category": failure_category,
                "error_excerpt": _read_text_tail(log_path),
            }
            _write_json(results_path, payload)
            return stage_exit_code

    warnings: List[str] = []
    resolved_python = py_res.python_executable if py_res else ""
    if py_res and py_res.warnings:
        warnings.extend(py_res.warnings)

    if args.skip:
        status = "skipped"
        skip_reason = args.skip_reason
        stage_exit_code = 0
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"[runner] stage skipped. reason={skip_reason}\n")
            if args.decision_reason:
                f.write(f"[runner] decision_reason={args.decision_reason}\n")
        payload = {
            "status": status,
            "skip_reason": skip_reason,
            "exit_code": stage_exit_code,
            "stage": args.stage,
            "task": args.task,
            "command": "",
            "timeout_sec": timeout_sec,
            "framework": args.framework,
            "assets": assets,
            "meta": {
                "python": f"{resolved_python} ({_python_version(resolved_python)})" if resolved_python else "",
                "git_commit": _try_git_commit(repo_root),
                "env_vars": _safe_env_snapshot(),
                "decision_reason": args.decision_reason,
                "timestamp_utc": _utc_now_iso(),
                "warnings": warnings,
            },
            "failure_category": "",
            "error_excerpt": "",
        }
        _write_json(results_path, payload)
        return 0

    cmd: List[str]
    if args.python_script:
        if not py_res:
            status = "failure"
            stage_exit_code = 1
            failure_category = "missing_report"
            with log_path.open("w", encoding="utf-8") as f:
                f.write("[runner] python_script requested but python resolution failed.\n")
            payload = {
                "status": status,
                "skip_reason": "unknown",
                "exit_code": stage_exit_code,
                "stage": args.stage,
                "task": args.task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": args.framework,
                "assets": assets,
                "meta": {
                    "python": "",
                    "git_commit": _try_git_commit(repo_root),
                    "env_vars": _safe_env_snapshot(),
                    "decision_reason": args.decision_reason,
                    "timestamp_utc": _utc_now_iso(),
                    "warnings": warnings,
                },
                "failure_category": failure_category,
                "error_excerpt": _read_text_tail(log_path),
            }
            _write_json(results_path, payload)
            return 1
        cmd = [py_res.python_executable, args.python_script] + args.command
    elif args.python_module:
        if not py_res:
            status = "failure"
            stage_exit_code = 1
            failure_category = "missing_report"
            with log_path.open("w", encoding="utf-8") as f:
                f.write("[runner] python_module requested but python resolution failed.\n")
            payload = {
                "status": status,
                "skip_reason": "unknown",
                "exit_code": stage_exit_code,
                "stage": args.stage,
                "task": args.task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": args.framework,
                "assets": assets,
                "meta": {
                    "python": "",
                    "git_commit": _try_git_commit(repo_root),
                    "env_vars": _safe_env_snapshot(),
                    "decision_reason": args.decision_reason,
                    "timestamp_utc": _utc_now_iso(),
                    "warnings": warnings,
                },
                "failure_category": failure_category,
                "error_excerpt": _read_text_tail(log_path),
            }
            _write_json(results_path, payload)
            return 1
        cmd = [py_res.python_executable, "-m", args.python_module] + args.command
    else:
        if not args.command:
            with log_path.open("w", encoding="utf-8") as f:
                f.write("[runner] No command provided.\n")
            status = "failure"
            stage_exit_code = 1
            failure_category = "entrypoint_not_found"
            payload = {
                "status": status,
                "skip_reason": "unknown",
                "exit_code": stage_exit_code,
                "stage": args.stage,
                "task": args.task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": args.framework,
                "assets": assets,
                "meta": {
                    "python": f"{resolved_python} ({_python_version(resolved_python)})" if resolved_python else "",
                    "git_commit": _try_git_commit(repo_root),
                    "env_vars": _safe_env_snapshot(),
                    "decision_reason": args.decision_reason,
                    "timestamp_utc": _utc_now_iso(),
                    "warnings": warnings,
                },
                "failure_category": failure_category,
                "error_excerpt": _read_text_tail(log_path),
            }
            _write_json(results_path, payload)
            return 1
        cmd = args.command

    command_str = " ".join(shlex.quote(c) for c in cmd)

    start = time.time()
    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[runner] repo_root={repo_root}\n")
        log_f.write(f"[runner] stage={args.stage} task={args.task} framework={args.framework}\n")
        log_f.write(f"[runner] timeout_sec={timeout_sec}\n")
        if py_res:
            log_f.write(f"[runner] resolved_python={py_res.python_executable} source={py_res.source}\n")
            if warnings:
                log_f.write("[runner] warnings:\n")
                for w in warnings:
                    log_f.write(f"  - {w}\n")
        log_f.write(f"[runner] command={command_str}\n\n")
        log_f.flush()

        try:
            cp = subprocess.run(
                cmd,
                cwd=str(repo_root),
                stdout=log_f,
                stderr=log_f,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
            command_returncode = int(cp.returncode)
        except subprocess.TimeoutExpired:
            timed_out = True
            command_returncode = None
        except FileNotFoundError:
            program_not_found = True
            command_returncode = None
        except Exception as e:
            log_f.write(f"\n[runner] Exception: {e}\n")
            command_returncode = None

    elapsed = time.time() - start
    log_tail = _read_text_tail(log_path)

    if timed_out or program_not_found or (command_returncode is not None and command_returncode != 0):
        status = "failure"
        stage_exit_code = 1
        failure_category = _classify_failure(log_tail, timed_out, program_not_found)
    else:
        status = "success"
        stage_exit_code = 0
        failure_category = ""

    meta_python = ""
    if py_res:
        meta_python = f"{py_res.python_executable} ({_python_version(py_res.python_executable)})"
    else:
        meta_python = f"{sys.executable} ({platform.python_version()})"

    payload = {
        "status": status,
        "skip_reason": skip_reason if status == "skipped" else "unknown",
        "exit_code": stage_exit_code,
        "stage": args.stage,
        "task": args.task,
        "command": command_str,
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": assets,
        "meta": {
            "python": meta_python,
            "git_commit": _try_git_commit(repo_root),
            "env_vars": _safe_env_snapshot(),
            "decision_reason": args.decision_reason,
            "timestamp_utc": _utc_now_iso(),
            "elapsed_sec": round(elapsed, 3),
            "command_returncode": command_returncode,
            "warnings": warnings,
        },
        "failure_category": failure_category,
        "error_excerpt": log_tail,
    }
    _write_json(results_path, payload)
    return stage_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
