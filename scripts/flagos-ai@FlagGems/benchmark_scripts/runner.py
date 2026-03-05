#!/usr/bin/env python3
"""
Unified benchmark command runner.

Responsibilities:
  - Execute a command from repo root with a timeout.
  - Capture stdout/stderr into build_output/<stage>/log.txt
  - Write build_output/<stage>/results.json (even on failure)

Exit codes:
  - 0: success or skipped
  - 1: failure
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
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_TIMEOUTS_SEC: Dict[str, int] = {
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

# Internal command exit codes that can be mapped to a more specific failure_category.
INTERNAL_EXITCODE_TO_FAILURE_CATEGORY: Dict[int, str] = {
    11: "auth_required",
    12: "download_failed",
    13: "deps",
    14: "data",
    15: "model",
    16: "args_unknown",
}


class RunnerError(Exception):
    pass


class MissingReportError(RunnerError):
    pass


class InvalidReportError(RunnerError):
    pass


@dataclass(frozen=True)
class PythonResolution:
    python: Optional[str]
    source: str
    report_path: str
    warnings: List[str]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _shlex_join(cmd: List[str]) -> str:
    try:
        return shlex.join(cmd)
    except Exception:
        return " ".join(cmd)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-max_lines:]
        return "\n".join(tail)
    except Exception:
        return ""


def _git_commit(repo_root: Path) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if p.returncode == 0:
            return p.stdout.strip()
    except Exception:
        pass
    return ""


def _default_report_path(cli_value: Optional[str]) -> str:
    if cli_value:
        return cli_value
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    return env if env else "/opt/scimlopsbench/report.json"


def _is_executable_file(path: str) -> bool:
    try:
        p = Path(path)
        return p.exists() and os.access(str(p), os.X_OK) and p.is_file()
    except Exception:
        return False


def _resolve_python(
    *,
    cli_python: Optional[str],
    python_required: bool,
    report_path: str,
) -> PythonResolution:
    warnings: List[str] = []

    if cli_python:
        return PythonResolution(
            python=cli_python, source="cli", report_path=report_path, warnings=warnings
        )

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return PythonResolution(
            python=env_python,
            source="env:SCIMLOPSBENCH_PYTHON",
            report_path=report_path,
            warnings=warnings,
        )

    report_file = Path(report_path)
    if report_file.exists():
        try:
            report = _read_json(report_file)
        except Exception as e:
            if python_required:
                raise InvalidReportError(f"Invalid report JSON: {report_path}: {e}")
            warnings.append(f"invalid_report_json:{report_path}")
            return PythonResolution(
                python=None, source="none", report_path=report_path, warnings=warnings
            )

        python_path = report.get("python_path")
        if isinstance(python_path, str) and python_path.strip():
            return PythonResolution(
                python=python_path.strip(),
                source="report:python_path",
                report_path=report_path,
                warnings=warnings,
            )

        if python_required:
            raise InvalidReportError(f"report.json missing python_path: {report_path}")
        warnings.append("report_missing_python_path")
        return PythonResolution(
            python=None, source="none", report_path=report_path, warnings=warnings
        )

    if python_required:
        raise MissingReportError(f"Missing report.json: {report_path}")

    # Stages that do not require Python are allowed to proceed without a report.
    warnings.append(f"missing_report:{report_path}")
    return PythonResolution(
        python=None, source="none", report_path=report_path, warnings=warnings
    )


def _load_assets_from_prepare(repo_root: Path) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    prepare_results = repo_root / "build_output" / "prepare" / "results.json"
    if not prepare_results.exists():
        warnings.append("missing_prepare_results")
        return (
            {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            warnings,
        )
    try:
        data = _read_json(prepare_results)
        assets = data.get("assets", {})
        dataset = assets.get("dataset", {})
        model = assets.get("model", {})
        return (
            {
                "dataset": {
                    "path": str(dataset.get("path", "")),
                    "source": str(dataset.get("source", "")),
                    "version": str(dataset.get("version", "")),
                    "sha256": str(dataset.get("sha256", "")),
                },
                "model": {
                    "path": str(model.get("path", "")),
                    "source": str(model.get("source", "")),
                    "version": str(model.get("version", "")),
                    "sha256": str(model.get("sha256", "")),
                },
            },
            warnings,
        )
    except Exception as e:
        warnings.append(f"invalid_prepare_results:{e}")
        return (
            {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            warnings,
        )


def _merge_dict(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in src.items():
        if (
            k in dst
            and isinstance(dst[k], dict)
            and isinstance(v, dict)
            and dst[k] is not None
        ):
            dst[k] = _merge_dict(dst[k], v)
        else:
            dst[k] = v
    return dst


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="runner.py",
        description="Run a benchmark stage command and write log/results artifacts.",
    )

    parser.add_argument(
        "--print-python",
        action="store_true",
        help="Resolve the stage python interpreter and print it to stdout.",
    )
    parser.add_argument("--python", default=None, help="Explicit python executable.")
    parser.add_argument(
        "--python-required",
        action="store_true",
        help="Fail if python cannot be resolved from report.json (unless --python is provided).",
    )
    parser.add_argument(
        "--report-path",
        default=None,
        help="Path to agent report.json (default: $SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json).",
    )

    parser.add_argument("--stage", default="", help="Stage name (e.g., prepare).")
    parser.add_argument("--task", default="", help="Task type (train|infer|check|download|validate).")
    parser.add_argument(
        "--framework",
        default="unknown",
        help="Framework (pytorch|tensorflow|jax|unknown).",
    )
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for this stage (default: build_output/<stage>).",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for command (default: repo root).",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Environment variable override KEY=VALUE (repeatable).",
    )
    parser.add_argument(
        "--decision-reason",
        default="",
        help="Why this entrypoint/params/data/model were chosen.",
    )

    parser.add_argument(
        "--assets-from-prepare",
        action="store_true",
        help="Populate assets from build_output/prepare/results.json.",
    )
    parser.add_argument(
        "--assets-json-path",
        default=None,
        help="Path to a JSON file containing an 'assets' object to include in results.json.",
    )
    parser.add_argument(
        "--extra-meta-json-path",
        default=None,
        help="Path to a JSON file merged into results.json.meta.",
    )

    parser.add_argument(
        "--skip-reason",
        default="",
        help="If set, skip this stage without running a command (repo_not_supported|insufficient_hardware|not_applicable|unknown).",
    )
    parser.add_argument(
        "--failure-category",
        default="",
        help="Override failure_category if the command fails.",
    )

    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    repo_root = _repo_root()
    report_path = _default_report_path(args.report_path)

    if args.print_python:
        try:
            py_res = _resolve_python(
                cli_python=args.python,
                python_required=True,
                report_path=report_path,
            )
        except Exception:
            print("", end="")
            return 1
        if not py_res.python:
            print("", end="")
            return 1
        print(py_res.python)
        return 0

    if not args.stage or not args.task:
        print(
            "runner.py: --stage and --task are required (unless --print-python).",
            file=sys.stderr,
        )
        return 1

    stage = args.stage
    out_dir = (
        Path(args.out_dir).resolve()
        if args.out_dir
        else (repo_root / "build_output" / stage)
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = (
        int(args.timeout_sec)
        if args.timeout_sec is not None
        else int(DEFAULT_TIMEOUTS_SEC.get(stage, 600))
    )

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    # Prepare base results skeleton.
    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": args.task,
        "command": "",
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo_root),
            "env_vars": {},
            "decision_reason": args.decision_reason,
            "timestamp_utc": _utc_timestamp(),
            "python_resolution": {
                "report_path": report_path,
                "source": "",
                "python": "",
                "warnings": [],
            },
            "warnings": [],
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    # Resolve python after we've established output paths so we can still write results.json on failure.
    try:
        py_res = _resolve_python(
            cli_python=args.python,
            python_required=args.python_required,
            report_path=report_path,
        )
        results["meta"]["python_resolution"] = {
            "report_path": py_res.report_path,
            "source": py_res.source,
            "python": py_res.python or "",
            "warnings": py_res.warnings,
        }
    except MissingReportError as e:
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"[runner] MISSING REPORT: {e}\n", encoding="utf-8")
        results["command"] = _shlex_join(cmd) if cmd else ""
        results["status"] = "failure"
        results["exit_code"] = 1
        results["failure_category"] = "missing_report"
        results["error_excerpt"] = _tail_lines(log_path)
        results_path.write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return 1
    except InvalidReportError as e:
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"[runner] INVALID REPORT: {e}\n", encoding="utf-8")
        results["command"] = _shlex_join(cmd) if cmd else ""
        results["status"] = "failure"
        results["exit_code"] = 1
        results["failure_category"] = "invalid_json"
        results["error_excerpt"] = _tail_lines(log_path)
        results_path.write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return 1

    if args.assets_from_prepare:
        assets, asset_warnings = _load_assets_from_prepare(repo_root)
        results["assets"] = assets
        if asset_warnings:
            results["meta"]["warnings"].extend(asset_warnings)

    # These JSON files are commonly produced by the executed command (e.g. prepare stage),
    # so we load them after command execution.
    post_assets_path: Optional[Path] = None
    post_extra_meta_path: Optional[Path] = None
    if args.assets_json_path:
        post_assets_path = (
            (repo_root / args.assets_json_path).resolve()
            if not os.path.isabs(args.assets_json_path)
            else Path(args.assets_json_path)
        )
    if args.extra_meta_json_path:
        post_extra_meta_path = (
            (repo_root / args.extra_meta_json_path).resolve()
            if not os.path.isabs(args.extra_meta_json_path)
            else Path(args.extra_meta_json_path)
        )

    # Apply env overrides.
    env_overrides: Dict[str, str] = {}
    for item in args.env:
        if "=" not in item:
            results["meta"]["warnings"].append(f"invalid_env_override:{item}")
            continue
        k, v = item.split("=", 1)
        env_overrides[k] = v
    results["meta"]["env_vars"] = env_overrides

    # Skip mode: no command executed.
    if args.skip_reason:
        results["status"] = "skipped"
        results["skip_reason"] = args.skip_reason
        results["exit_code"] = 0
        results["command"] = "(skipped)"
        results["failure_category"] = "unknown"
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"[runner] stage={stage} skipped: {args.skip_reason}\n")
            if args.decision_reason:
                f.write(f"[runner] decision_reason: {args.decision_reason}\n")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0

    if not cmd:
        with log_path.open("w", encoding="utf-8") as f:
            f.write("[runner] No command provided.\n")
        results["command"] = ""
        results["failure_category"] = "args_unknown"
        results["error_excerpt"] = _tail_lines(log_path)
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    # Substitute {python} placeholder if present.
    if any(part == "{python}" for part in cmd):
        if not py_res.python:
            # Only allow PATH fallback for stages that do not require Python.
            fallback = shutil.which("python") or shutil.which("python3") or sys.executable
            results["meta"]["warnings"].append("python_fallback_from_PATH")
            cmd = [fallback if part == "{python}" else part for part in cmd]
            results["meta"]["python_resolution"]["source"] = "PATH_fallback"
            results["meta"]["python_resolution"]["python"] = fallback
        else:
            cmd = [py_res.python if part == "{python}" else part for part in cmd]

    # Validate resolved python executable if used explicitly.
    if args.python_required and py_res.python and not _is_executable_file(py_res.python):
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"[runner] Resolved python is not executable: {py_res.python}\n")
        results["command"] = _shlex_join(cmd)
        results["failure_category"] = "path_hallucination"
        results["error_excerpt"] = _tail_lines(log_path)
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    cwd = Path(args.cwd).resolve() if args.cwd else repo_root

    # Execute command.
    start = time.time()
    command_exit_code = 1
    failure_category = "unknown"
    try:
        env = os.environ.copy()
        env.update(env_overrides)
        env["SCIMLOPSBENCH_REPO_ROOT"] = str(repo_root)

        with log_path.open("w", encoding="utf-8") as log_f:
            log_f.write(f"[runner] stage={stage}\n")
            log_f.write(f"[runner] cwd={cwd}\n")
            log_f.write(f"[runner] timeout_sec={timeout_sec}\n")
            log_f.write(f"[runner] command={_shlex_join(cmd)}\n")
            log_f.flush()

            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
            command_exit_code = int(proc.returncode)
            if command_exit_code == 0:
                failure_category = "unknown"
            else:
                failure_category = INTERNAL_EXITCODE_TO_FAILURE_CATEGORY.get(
                    command_exit_code, "runtime"
                )
    except subprocess.TimeoutExpired:
        command_exit_code = 124
        failure_category = "timeout"
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write("\n[runner] TIMEOUT\n")
    except FileNotFoundError as e:
        command_exit_code = 127
        failure_category = "entrypoint_not_found"
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write(f"\n[runner] FILE NOT FOUND: {e}\n")
    except MissingReportError as e:
        command_exit_code = 1
        failure_category = "missing_report"
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write(f"\n[runner] MISSING REPORT: {e}\n")
    except InvalidReportError as e:
        command_exit_code = 1
        failure_category = "invalid_json"
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write(f"\n[runner] INVALID REPORT: {e}\n")
    except Exception as e:
        command_exit_code = 1
        failure_category = "unknown"
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write(f"\n[runner] UNHANDLED ERROR: {e}\n")
    finally:
        end = time.time()
        duration = end - start

        results["command"] = _shlex_join(cmd)
        results["meta"]["duration_sec"] = round(duration, 6)
        results["meta"]["command_exit_code"] = command_exit_code

        # Post-load assets/meta JSON produced by the command.
        if post_assets_path is not None:
            if post_assets_path.exists():
                try:
                    assets_payload = _read_json(post_assets_path)
                    if (
                        isinstance(assets_payload, dict)
                        and "assets" in assets_payload
                        and isinstance(assets_payload["assets"], dict)
                    ):
                        results["assets"] = assets_payload["assets"]
                    elif (
                        isinstance(assets_payload, dict)
                        and "dataset" in assets_payload
                        and "model" in assets_payload
                    ):
                        results["assets"] = assets_payload
                    else:
                        results["meta"]["warnings"].append(
                            f"assets_json_unrecognized:{post_assets_path}"
                        )
                except Exception as e:
                    results["meta"]["warnings"].append(
                        f"assets_json_read_failed:{post_assets_path}:{e}"
                    )
            else:
                if command_exit_code == 0:
                    results["meta"]["warnings"].append(
                        f"assets_json_missing_after_success:{post_assets_path}"
                    )

        if post_extra_meta_path is not None:
            if post_extra_meta_path.exists():
                try:
                    extra = _read_json(post_extra_meta_path)
                    if isinstance(extra, dict):
                        results["meta"] = _merge_dict(results["meta"], extra)
                    else:
                        results["meta"]["warnings"].append(
                            f"extra_meta_not_dict:{post_extra_meta_path}"
                        )
                except Exception as e:
                    results["meta"]["warnings"].append(
                        f"extra_meta_read_failed:{post_extra_meta_path}:{e}"
                    )
            else:
                if command_exit_code == 0:
                    results["meta"]["warnings"].append(
                        f"extra_meta_missing_after_success:{post_extra_meta_path}"
                    )

        if command_exit_code == 0:
            results["status"] = "success"
            results["exit_code"] = 0
            results["skip_reason"] = "not_applicable"
            results["failure_category"] = "unknown"
            results["error_excerpt"] = ""
        else:
            results["status"] = "failure"
            results["exit_code"] = 1
            results["skip_reason"] = "not_applicable"
            results["failure_category"] = args.failure_category or failure_category
            results["error_excerpt"] = _tail_lines(log_path)

        # Persist results.json always.
        try:
            results_path.write_text(
                json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            # Last resort: print to stderr.
            print(json.dumps(results, indent=2, ensure_ascii=False), file=sys.stderr)

    return 0 if results["status"] != "failure" else 1


if __name__ == "__main__":
    raise SystemExit(main())
