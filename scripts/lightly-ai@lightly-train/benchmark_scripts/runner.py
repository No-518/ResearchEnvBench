#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")
ENV_REPORT_PATH = "SCIMLOPSBENCH_REPORT"
ENV_PYTHON = "SCIMLOPSBENCH_PYTHON"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_root() -> Path:
    # benchmark_scripts/runner.py -> repo root
    return Path(__file__).resolve().parent.parent


def _safe_json_load(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"report not found: {path}"
    except Exception as exc:  # noqa: BLE001
        return None, f"failed to parse json {path}: {exc}"


def _is_executable_file(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(path, os.X_OK)
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class ResolvedPython:
    python_path: Path
    source: str  # cli|env|report|path_fallback
    report_path: Path | None
    warnings: list[str]


def resolve_python(
    *,
    cli_python: str | None,
    cli_report_path: str | None,
    allow_path_fallback: bool,
) -> tuple[ResolvedPython | None, str | None, str | None]:
    """Resolve python interpreter for benchmarks.

    Returns: (resolved, failure_category, error_message)
    """
    warnings: list[str] = []

    if cli_python:
        python_path = Path(cli_python)
        if not _is_executable_file(python_path):
            return None, "missing_report", f"--python is not executable: {python_path}"
        return (
            ResolvedPython(
                python_path=python_path.resolve(),
                source="cli",
                report_path=None,
                warnings=[],
            ),
            None,
            None,
        )

    env_python = os.environ.get(ENV_PYTHON)
    if env_python:
        python_path = Path(env_python)
        if not _is_executable_file(python_path):
            return None, "missing_report", f"{ENV_PYTHON} is not executable: {python_path}"
        return (
            ResolvedPython(
                python_path=python_path.resolve(),
                source="env",
                report_path=None,
                warnings=[],
            ),
            None,
            None,
        )

    report_path = (
        Path(cli_report_path)
        if cli_report_path
        else Path(os.environ.get(ENV_REPORT_PATH, str(DEFAULT_REPORT_PATH)))
    )
    report, report_err = _safe_json_load(report_path)
    if report is None:
        # Strict: no fallback if report is missing/invalid.
        return None, "missing_report", report_err

    raw_python_path = report.get("python_path")
    if not isinstance(raw_python_path, str) or not raw_python_path.strip():
        if allow_path_fallback:
            fallback = shutil.which("python3") or shutil.which("python")
            if fallback:
                warnings.append("report missing python_path; fell back to python from PATH")
                return (
                    ResolvedPython(
                        python_path=Path(fallback),
                        source="path_fallback",
                        report_path=report_path,
                        warnings=warnings,
                    ),
                    None,
                    None,
                )
        return None, "missing_report", f"report missing python_path: {report_path}"

    python_path = Path(raw_python_path)
    if _is_executable_file(python_path):
        return (
            ResolvedPython(
                python_path=python_path.resolve(),
                source="report",
                report_path=report_path,
                warnings=[],
            ),
            None,
            None,
        )

    if allow_path_fallback:
        fallback = shutil.which("python3") or shutil.which("python")
        if fallback:
            warnings.append(
                f"report python_path not executable ({python_path}); fell back to python from PATH"
            )
            return (
                ResolvedPython(
                    python_path=Path(fallback),
                    source="path_fallback",
                    report_path=report_path,
                    warnings=warnings,
                ),
                None,
                None,
            )

    return None, "missing_report", f"python_path not executable: {python_path}"


def _git_commit(repo_root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _tail_text(path: Path, max_lines: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            dq: deque[str] = deque(f, maxlen=max_lines)
        return "".join(dq).strip()
    except Exception:  # noqa: BLE001
        return ""


def _cmd_to_str(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _select_env_vars() -> dict[str, str]:
    keep = [
        "CUDA_VISIBLE_DEVICES",
        "NCCL_DEBUG",
        "NCCL_SOCKET_IFNAME",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "PYTHONPATH",
        ENV_REPORT_PATH,
        ENV_PYTHON,
        "LIGHTLY_TRAIN_CACHE_DIR",
        "LIGHTLY_TRAIN_MODEL_CACHE_DIR",
        "LIGHTLY_TRAIN_DATA_CACHE_DIR",
        "LIGHTLY_TRAIN_EVENTS_DISABLED",
        "XDG_CACHE_HOME",
        "HF_HOME",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
    ]
    out: dict[str, str] = {}
    for k in keep:
        v = os.environ.get(k)
        if v is not None:
            out[k] = v
    return out


def _run_command(
    *,
    stage: str,
    task: str,
    cmd: list[str],
    timeout_sec: int,
    framework: str,
    results_path: Path,
    log_path: Path,
    assets: dict[str, Any] | None,
    decision_reason: str,
    require_python: bool,
    cli_python: str | None,
    cli_report_path: str | None,
    allow_path_fallback: bool,
    no_run: bool,
    status_override: str | None,
    skip_reason: str,
    failure_category_override: str | None,
    error_override: str | None,
    extra_meta: dict[str, Any] | None,
) -> int:
    repo_root = _repo_root()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.parent.mkdir(parents=True, exist_ok=True)

    resolved_py: ResolvedPython | None = None
    py_fail_cat: str | None = None
    py_err: str | None = None
    resolved_py, py_fail_cat, py_err = resolve_python(
        cli_python=cli_python,
        cli_report_path=cli_report_path,
        allow_path_fallback=allow_path_fallback,
    )

    if require_python and resolved_py is None:
        payload: dict[str, Any] = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": stage,
            "task": task,
            "command": _cmd_to_str(cmd) if cmd else "",
            "timeout_sec": timeout_sec,
            "framework": framework,
            "assets": assets
            or {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "meta": {
                "python": None,
                "git_commit": _git_commit(repo_root),
                "env_vars": _select_env_vars(),
                "decision_reason": decision_reason,
                "python_resolution": {"failure_category": py_fail_cat, "error": py_err},
                **(extra_meta or {}),
            },
            "failure_category": "missing_report",
            "error_excerpt": (py_err or "")[:4000],
        }
        _write_json(results_path, payload)
        log_path.write_text((py_err or "") + "\n", encoding="utf-8")
        return 1

    started_utc = _utc_now_iso()

    # Allow stage scripts to generate results without running a command.
    if no_run:
        status = status_override or "failure"
        exit_code = 0 if status in ("success", "skipped") else 1
        failure_category = "" if status != "failure" else (failure_category_override or "unknown")
        payload = {
            "status": status,
            "skip_reason": skip_reason if status == "skipped" else "unknown",
            "exit_code": exit_code,
            "stage": stage,
            "task": task,
            "command": _cmd_to_str(cmd) if cmd else "",
            "timeout_sec": timeout_sec,
            "framework": framework,
            "assets": assets
            or {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "meta": {
                "python": str(resolved_py.python_path) if resolved_py else None,
                "python_resolution": {
                    "source": resolved_py.source if resolved_py else None,
                    "report_path": str(resolved_py.report_path) if resolved_py and resolved_py.report_path else None,
                    "warnings": resolved_py.warnings if resolved_py else [],
                },
                "git_commit": _git_commit(repo_root),
                "env_vars": _select_env_vars(),
                "decision_reason": decision_reason,
                "started_utc": started_utc,
                "finished_utc": _utc_now_iso(),
                **(extra_meta or {}),
            },
            "failure_category": failure_category,
            "error_excerpt": (error_override or "")[:4000],
        }
        log_path.write_text((error_override or "") + "\n", encoding="utf-8")
        _write_json(results_path, payload)
        return exit_code

    env = os.environ.copy()
    if resolved_py is not None:
        # Expose resolved python to subprocesses; do not override if user set it.
        env.setdefault("SCIMLOPSBENCH_PYTHON_RESOLVED", str(resolved_py.python_path))
        env.setdefault("SCIMLOPSBENCH_REPORT_RESOLVED", str(resolved_py.report_path or ""))

    status = "failure"
    exit_code = 1
    failure_category = "unknown"
    timed_out = False

    with log_path.open("wb") as log_fp:
        header = (
            f"[runner] stage={stage} task={task}\n"
            f"[runner] started_utc={started_utc}\n"
            f"[runner] cwd={repo_root}\n"
            f"[runner] command={_cmd_to_str(cmd)}\n"
        )
        log_fp.write(header.encode("utf-8", errors="replace"))
        log_fp.flush()

        try:
            proc = subprocess.run(
                cmd,
                cwd=repo_root,
                env=env,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout_sec,
            )
            cmd_rc = int(proc.returncode)
        except subprocess.TimeoutExpired:
            timed_out = True
            cmd_rc = 124
        except FileNotFoundError as exc:
            cmd_rc = 127
            failure_category = "entrypoint_not_found"
            log_fp.write(f"\n[runner] FileNotFoundError: {exc}\n".encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            cmd_rc = 1
            failure_category = "runtime"
            log_fp.write(f"\n[runner] Exception: {exc}\n".encode("utf-8"))

        if timed_out:
            failure_category = "timeout"
            status = "failure"
            exit_code = 1
        elif cmd_rc == 0:
            status = "success"
            exit_code = 0
            failure_category = ""
        else:
            status = "failure"
            exit_code = 1
            failure_category = failure_category_override or failure_category or "runtime"

        finished_utc = _utc_now_iso()
        footer = (
            f"\n[runner] finished_utc={finished_utc}\n"
            f"[runner] command_exit_code={cmd_rc}\n"
            f"[runner] stage_exit_code={exit_code}\n"
        )
        log_fp.write(footer.encode("utf-8", errors="replace"))

    if status_override is not None:
        status = status_override
        if status == "skipped":
            exit_code = 0
            failure_category = ""
        elif status == "success":
            exit_code = 0
            failure_category = ""
        else:
            exit_code = 1
            failure_category = failure_category_override or (failure_category or "unknown")

    error_excerpt = _tail_text(log_path, 220)
    if error_override:
        error_excerpt = error_override

    meta: dict[str, Any] = {
        "python": str(resolved_py.python_path) if resolved_py else None,
        "python_resolution": {
            "source": resolved_py.source if resolved_py else None,
            "report_path": str(resolved_py.report_path) if resolved_py and resolved_py.report_path else None,
            "warnings": resolved_py.warnings if resolved_py else [],
        },
        "git_commit": _git_commit(repo_root),
        "env_vars": _select_env_vars(),
        "decision_reason": decision_reason,
        "started_utc": started_utc,
        "finished_utc": _utc_now_iso(),
        "timed_out": timed_out,
        **(extra_meta or {}),
    }

    payload = {
        "status": status,
        "skip_reason": skip_reason if status == "skipped" else "unknown",
        "exit_code": exit_code,
        "stage": stage,
        "task": task,
        "command": _cmd_to_str(cmd),
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": assets
        or {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": meta,
        "failure_category": failure_category if status == "failure" else "",
        "error_excerpt": error_excerpt if status == "failure" else "",
    }
    _write_json(results_path, payload)
    return exit_code


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="runner.py")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_resolve = subparsers.add_parser("resolve-python", help="Print resolved python path")
    p_resolve.add_argument("--python", dest="python_bin", default=None)
    p_resolve.add_argument("--report-path", default=None)
    p_resolve.add_argument("--allow-path-fallback", action="store_true")

    p_run = subparsers.add_parser("run", help="Run a command with unified logging/results")
    p_run.add_argument("--stage", required=True)
    p_run.add_argument("--task", required=True)
    p_run.add_argument("--timeout-sec", type=int, required=True)
    p_run.add_argument("--framework", default="unknown")
    p_run.add_argument("--results-path", default=None)
    p_run.add_argument("--log-path", default=None)
    p_run.add_argument("--assets-from", default=None)
    p_run.add_argument("--decision-reason", default="")
    p_run.add_argument("--require-python", action="store_true")
    p_run.add_argument("--python", dest="python_bin", default=None)
    p_run.add_argument("--report-path", default=None)
    p_run.add_argument("--allow-path-fallback", action="store_true")
    p_run.add_argument("--no-run", action="store_true")
    p_run.add_argument("--status", choices=["success", "failure", "skipped"], default=None)
    p_run.add_argument(
        "--skip-reason",
        default="unknown",
        choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"],
    )
    p_run.add_argument(
        "--failure-category",
        default=None,
        choices=[
            "entrypoint_not_found",
            "args_unknown",
            "auth_required",
            "download_failed",
            "deps",
            "data",
            "model",
            "runtime",
            "oom",
            "timeout",
            "cpu_not_supported",
            "missing_report",
            "invalid_json",
            "unknown",
        ],
    )
    p_run.add_argument("--error", default=None)
    p_run.add_argument("--meta-json", default=None)
    p_run.add_argument("command", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)

    if args.cmd == "resolve-python":
        resolved, fail_cat, err = resolve_python(
            cli_python=args.python_bin,
            cli_report_path=args.report_path,
            allow_path_fallback=bool(args.allow_path_fallback),
        )
        if resolved is None:
            print(err or "failed to resolve python", file=sys.stderr)
            return 1
        print(str(resolved.python_path))
        return 0

    if args.cmd == "run":
        stage = str(args.stage)
        task = str(args.task)
        timeout_sec = int(args.timeout_sec)
        framework = str(args.framework)

        if not args.command:
            print("runner.py run: missing command after options", file=sys.stderr)
            return 2
        if args.command and args.command[0] == "--":
            cmd = args.command[1:]
        else:
            cmd = args.command

        out_dir = _repo_root() / "build_output" / stage
        results_path = Path(args.results_path) if args.results_path else (out_dir / "results.json")
        log_path = Path(args.log_path) if args.log_path else (out_dir / "log.txt")

        assets: dict[str, Any] | None = None
        if args.assets_from:
            assets_json, assets_err = _safe_json_load(Path(args.assets_from))
            if assets_json and isinstance(assets_json.get("assets"), dict):
                assets = assets_json["assets"]
            elif assets_json and isinstance(assets_json.get("dataset"), dict) and isinstance(
                assets_json.get("model"), dict
            ):
                assets = assets_json  # already an assets object
            elif assets_err:
                assets = None

        extra_meta: dict[str, Any] | None = None
        if args.meta_json:
            meta_obj, _ = _safe_json_load(Path(args.meta_json))
            if isinstance(meta_obj, dict):
                extra_meta = meta_obj

        return _run_command(
            stage=stage,
            task=task,
            cmd=[str(c) for c in cmd],
            timeout_sec=timeout_sec,
            framework=framework,
            results_path=results_path,
            log_path=log_path,
            assets=assets,
            decision_reason=str(args.decision_reason),
            require_python=bool(args.require_python),
            cli_python=args.python_bin,
            cli_report_path=args.report_path,
            allow_path_fallback=bool(args.allow_path_fallback),
            no_run=bool(args.no_run),
            status_override=args.status,
            skip_reason=str(args.skip_reason),
            failure_category_override=args.failure_category,
            error_override=args.error,
            extra_meta=extra_meta,
        )

    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

