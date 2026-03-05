#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
from typing import Any


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_text_tail(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = text.splitlines()
    tail = lines[-max_lines:]
    return "\n".join(tail).strip()


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def _report_path(cli_report_path: str | None) -> Path:
    if cli_report_path and cli_report_path.strip():
        return Path(cli_report_path.strip())
    env_report = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_report and env_report.strip():
        return Path(env_report.strip())
    return Path("/opt/scimlopsbench/report.json")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_python(
    *,
    cli_python: str | None,
    requires_python: bool,
    report_path: Path,
) -> tuple[str | None, dict[str, Any]]:
    meta: dict[str, Any] = {
        "resolved_python": None,
        "resolved_python_source": None,
        "resolved_python_warning": "",
        "report_path": str(report_path),
    }

    if cli_python:
        meta["resolved_python"] = cli_python
        meta["resolved_python_source"] = "cli"
        return cli_python, meta

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        meta["resolved_python"] = env_python
        meta["resolved_python_source"] = "env:SCIMLOPSBENCH_PYTHON"
        return env_python, meta

    # Report lookup (default /opt/scimlopsbench/report.json; can be overridden).
    try:
        report_text = report_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        meta["resolved_python_warning"] = "report_missing"
    except PermissionError as e:
        meta["resolved_python_warning"] = f"report_permission_error: {e}"
    except Exception as e:
        meta["resolved_python_warning"] = f"report_read_error: {e}"
    else:
        try:
            report = json.loads(report_text)
            python_path = report.get("python_path")
            if isinstance(python_path, str) and python_path.strip():
                meta["resolved_python"] = python_path.strip()
                meta["resolved_python_source"] = "report:python_path"
                return meta["resolved_python"], meta
        except Exception as e:
            meta["resolved_python_warning"] = f"failed_to_parse_report: {e}"

    # If the report is missing/invalid and Python is required, fail fast.
    if requires_python:
        meta["resolved_python_source"] = "unresolved"
        return None, meta

    meta["resolved_python_source"] = "none"
    return None, meta


def _resolve_command_python(command: list[str], resolved_python: str | None) -> tuple[list[str], bool]:
    if not command:
        return command, False
    if not resolved_python:
        return command, False

    replaced = False
    out: list[str] = []
    for idx, tok in enumerate(command):
        if tok == "{python}":
            out.append(resolved_python)
            replaced = True
            continue
        if idx == 0 and tok in {"python", "python3"}:
            out.append(resolved_python)
            replaced = True
            continue
        out.append(tok)
    return out, replaced


def _python_looks_executable(python_path: str) -> bool:
    p = Path(python_path)
    if not p.exists():
        return False
    if p.is_dir():
        return False
    return os.access(str(p), os.X_OK)


def _safe_env_snapshot() -> dict[str, str]:
    keys = sorted(
        {
            k
            for k in os.environ.keys()
            if k.startswith("SCIMLOPSBENCH_")
            or k
            in {
                "CUDA_VISIBLE_DEVICES",
                "XPU_VISIBLE_DEVICES",
                "TORCHFT_LIGHTHOUSE",
                "REPLICA_GROUP_ID",
                "NUM_REPLICA_GROUPS",
                "MASTER_ADDR",
                "MASTER_PORT",
                "RANK",
                "WORLD_SIZE",
            }
        }
    )
    return {k: os.environ.get(k, "") for k in keys}


def _default_timeout_sec(stage: str) -> int:
    return {
        "prepare": 1200,
        "cpu": 600,
        "cuda": 120,
        "single_gpu": 600,
        "multi_gpu": 1200,
        "env_size": 120,
        "hallucination": 120,
        "pyright": 600,
        "summary": 120,
    }.get(stage, 600)


def _load_assets_manifest(repo_root: Path) -> dict[str, Any]:
    manifest_path = repo_root / "benchmark_assets" / "manifest.json"
    if not manifest_path.exists():
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    try:
        data = _load_json(manifest_path)
        assets = data.get("assets", data)
        dataset = assets.get("dataset", {}) if isinstance(assets, dict) else {}
        model = assets.get("model", {}) if isinstance(assets, dict) else {}
        return {
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
        }
    except Exception:
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_command(
    *,
    repo_root: Path,
    command: list[str],
    timeout_sec: int,
    log_path: Path,
    env: dict[str, str],
) -> tuple[int, bool, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    failure_category = "unknown"

    with log_path.open("w", encoding="utf-8") as log_fp:
        log_fp.write(f"[runner] utc_start={_utc_timestamp()}\n")
        log_fp.write(f"[runner] cwd={repo_root}\n")
        log_fp.write(f"[runner] command={shlex.join(command)}\n")
        log_fp.flush()

        try:
            proc = subprocess.Popen(
                command,
                cwd=repo_root,
                stdout=log_fp,
                stderr=log_fp,
                text=True,
                start_new_session=True,
                env=env,
            )
        except FileNotFoundError:
            log_fp.write("[runner] error: executable_not_found\n")
            return 127, False, "entrypoint_not_found"
        except Exception as e:
            log_fp.write(f"[runner] error: failed_to_start_process: {e}\n")
            return 1, False, "runtime"

        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            failure_category = "timeout"
            log_fp.write(f"[runner] timeout_after_sec={timeout_sec}\n")
            log_fp.flush()
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

        rc = proc.returncode if proc.returncode is not None else 1

    if timed_out:
        return rc, True, failure_category

    if rc == 0:
        return 0, False, "not_applicable"

    return rc, False, "runtime"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="runner.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Unified executor that runs a command and writes build_output/<stage>/{log.txt,results.json}.",
        epilog=textwrap.dedent(
            """\
            Examples:
              python benchmark_scripts/runner.py --stage cpu --task train -- bash -lc 'echo hello'
              python benchmark_scripts/runner.py --stage single_gpu --task train --framework pytorch --timeout-sec 600 -- bash -lc '...'
            """
        ),
    )
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--framework", default="unknown")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--timeout-sec", type=int, default=0)
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python", default=None, help="Explicit python executable (highest priority).")
    parser.add_argument(
        "--requires-python",
        action="store_true",
        default=False,
        help="Fail if report/python cannot be resolved.",
    )
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--skip", action="store_true", help="Do not execute; just write skipped results.")
    parser.add_argument(
        "--skip-reason",
        default="unknown",
        choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"],
    )
    parser.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="Command to execute (prefix with --).",
    )

    args = parser.parse_args(argv)

    repo_root = _repo_root()
    stage = args.stage
    out_dir = Path(args.out_dir) if args.out_dir else (repo_root / "build_output" / stage)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = args.timeout_sec if args.timeout_sec > 0 else _default_timeout_sec(stage)
    report_path = _report_path(args.report_path)

    resolved_python, py_meta = _resolve_python(
        cli_python=args.python, requires_python=args.requires_python, report_path=report_path
    )

    base_assets = _load_assets_manifest(repo_root)

    command_list = list(args.cmd)
    # argparse.REMAINDER keeps a leading "--" separator; drop it if present.
    while command_list and command_list[0] == "--":
        command_list = command_list[1:]
    command_list, python_replaced = _resolve_command_python(command_list, resolved_python)
    command_str = shlex.join(command_list) if command_list else ""

    result: dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": args.task,
        "command": command_str,
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": base_assets,
        "meta": {
            "python": resolved_python or sys.executable,
            "runner_python": sys.executable,
            "timestamp_utc": _utc_timestamp(),
            "git_commit": _git_commit(repo_root),
            "env_vars": _safe_env_snapshot(),
            "decision_reason": args.decision_reason,
            "python_replaced_in_command": python_replaced,
            **py_meta,
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    def finalize_and_write(exit_code: int) -> int:
        result["exit_code"] = exit_code
        if exit_code == 0 and result["status"] != "failure":
            result["failure_category"] = "not_applicable"
        if exit_code != 0 and not result.get("error_excerpt"):
            result["error_excerpt"] = _read_text_tail(log_path)
        _write_json(results_path, result)
        return exit_code

    if args.skip:
        result["status"] = "skipped"
        result["skip_reason"] = args.skip_reason
        result["exit_code"] = 0
        result["failure_category"] = "not_applicable"
        return finalize_and_write(0)

    if args.requires_python and not resolved_python:
        log_path.write_text(
            f"[runner] error: missing_report_or_python (report_path={report_path})\n",
            encoding="utf-8",
        )
        result["status"] = "failure"
        result["failure_category"] = "missing_report"
        result["error_excerpt"] = _read_text_tail(log_path)
        return finalize_and_write(1)

    if args.requires_python and resolved_python and not _python_looks_executable(resolved_python):
        log_path.write_text(
            f"[runner] error: resolved_python_not_executable ({resolved_python})\n",
            encoding="utf-8",
        )
        result["status"] = "failure"
        result["failure_category"] = "deps"
        result["error_excerpt"] = _read_text_tail(log_path)
        return finalize_and_write(1)

    if not command_list:
        log_path.write_text("[runner] error: no_command_provided\n", encoding="utf-8")
        result["status"] = "failure"
        result["failure_category"] = "args_unknown"
        result["error_excerpt"] = _read_text_tail(log_path)
        return finalize_and_write(1)

    rc, timed_out, failure_category = _run_command(
        repo_root=repo_root,
        command=command_list,
        timeout_sec=timeout_sec,
        log_path=log_path,
        env={
            **os.environ.copy(),
            **(
                {"SCIMLOPSBENCH_RESOLVED_PYTHON": resolved_python}
                if resolved_python
                else {}
            ),
        },
    )

    # Map command return code to stage status.
    if timed_out:
        result["status"] = "failure"
        result["failure_category"] = failure_category
        result["meta"]["command_exit_code"] = rc
        result["error_excerpt"] = _read_text_tail(log_path)
        return finalize_and_write(1)

    if rc == 0:
        result["status"] = "success"
        result["skip_reason"] = "not_applicable"
        result["failure_category"] = "not_applicable"
        result["meta"]["command_exit_code"] = 0
        return finalize_and_write(0)

    # Failure path: categorize a bit.
    excerpt = _read_text_tail(log_path)
    result["status"] = "failure"
    result["skip_reason"] = "not_applicable"
    result["meta"]["command_exit_code"] = rc
    result["error_excerpt"] = excerpt

    lowered = excerpt.lower()
    if "out of memory" in lowered or "cuda out of memory" in lowered:
        result["failure_category"] = "oom"
    elif "no module named" in lowered or "modulenotfounderror" in lowered:
        result["failure_category"] = "deps"
    elif "permission denied" in lowered:
        result["failure_category"] = "deps"
    else:
        result["failure_category"] = failure_category

    return finalize_and_write(1)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
