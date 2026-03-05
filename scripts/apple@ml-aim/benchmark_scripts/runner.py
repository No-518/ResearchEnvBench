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
from typing import Any, Dict, List, Optional, Sequence, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_json_load(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError as e:
        return None, f"invalid_json: {e}"
    except Exception as e:  # pragma: no cover
        return None, f"read_error: {e}"


def _git_commit(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
        return out or None
    except Exception:
        return None


def _tail_lines(path: Path, *, max_lines: int = 200, max_bytes: int = 128_000) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start, os.SEEK_SET)
            chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-max_lines:])
    except FileNotFoundError:
        return ""
    except Exception as e:  # pragma: no cover
        return f"[tail_error] {e}"


def _is_executable_file(path: str) -> bool:
    try:
        p = Path(path)
        return p.exists() and p.is_file() and os.access(str(p), os.X_OK)
    except Exception:
        return False


@dataclass
class PythonResolution:
    python: str
    method: str  # cli|env|report|path_fallback
    report_path: Optional[str]
    warning: Optional[str] = None


def resolve_python(
    *,
    cli_python: Optional[str],
    needs_python: bool,
    report_path: Optional[str],
) -> Tuple[Optional[PythonResolution], Optional[Dict[str, Any]], Optional[str]]:
    """
    Returns (resolution, report_json, error_message).
    If needs_python is False, returns (None, report_json|None, None).
    """
    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")

    resolved_report_path = (
        report_path
        or os.environ.get("SCIMLOPSBENCH_REPORT")
        or DEFAULT_REPORT_PATH
    )
    report_file = Path(resolved_report_path)

    report_json: Optional[Dict[str, Any]] = None
    report_err: Optional[str] = None
    if report_file.exists():
        report_json, report_err = _safe_json_load(report_file)
        if report_err is not None and report_err != "missing":
            return None, None, f"invalid report.json at {resolved_report_path}: {report_err}"
    else:
        report_err = "missing"

    if not needs_python:
        return None, report_json, None

    if cli_python:
        if not _is_executable_file(cli_python):
            return None, report_json, f"--python is not an executable file: {cli_python}"
        return (
            PythonResolution(
                python=cli_python,
                method="cli",
                report_path=str(report_file),
            ),
            report_json,
            None,
        )

    if env_python:
        if not _is_executable_file(env_python):
            return (
                None,
                report_json,
                f"SCIMLOPSBENCH_PYTHON is not an executable file: {env_python}",
            )
        return (
            PythonResolution(
                python=env_python, method="env", report_path=str(report_file)
            ),
            report_json,
            None,
        )

    if report_err == "missing":
        return None, None, f"missing report.json at {resolved_report_path}"

    python_from_report = (report_json or {}).get("python_path")
    if isinstance(python_from_report, str) and python_from_report.strip():
        python_from_report = python_from_report.strip()
        if not _is_executable_file(python_from_report):
            return (
                None,
                report_json,
                f"python_path from report.json is not executable: {python_from_report}",
            )
        return (
            PythonResolution(
                python=python_from_report,
                method="report",
                report_path=str(report_file),
            ),
            report_json,
            None,
        )

    # Fallback to PATH python as last resort; keep a warning (validation should catch path hallucination).
    fallback = shutil.which("python") or shutil.which("python3")
    if not fallback:
        return None, report_json, "unable to find python on PATH"
    return (
        PythonResolution(
            python=fallback,
            method="path_fallback",
            report_path=str(report_file),
            warning="report.json missing python_path; fell back to python from PATH",
        ),
        report_json,
        None,
    )


def _detect_failure_category(log_excerpt: str, *, timed_out: bool, spawn_error: bool) -> str:
    if timed_out:
        return "timeout"
    if spawn_error:
        return "entrypoint_not_found"
    lowered = log_excerpt.lower()
    if "out of memory" in lowered or "cuda out of memory" in lowered:
        return "oom"
    if "no such file or directory" in lowered:
        return "entrypoint_not_found"
    return "runtime"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark command runner.")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument(
        "--framework",
        default="unknown",
        choices=("pytorch", "tensorflow", "jax", "unknown"),
    )
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python", dest="cli_python", default=None)
    parser.add_argument(
        "--needs-python",
        dest="needs_python",
        action="store_true",
        default=True,
        help="Require resolving python via report/env/cli (default: true).",
    )
    parser.add_argument(
        "--no-needs-python",
        dest="needs_python",
        action="store_false",
        help="Do not require python resolution (for pure shell stages).",
    )
    parser.add_argument("--assets-from", default=None)
    parser.add_argument("--decision-reason", default="")
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Extra env vars for the command: KEY=VALUE (repeatable).",
    )
    parser.add_argument(
        "--skip",
        action="store_true",
        help="Do not run command; write skipped results.json.",
    )
    parser.add_argument(
        "--skip-reason",
        default="unknown",
        choices=("repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"),
    )
    parser.add_argument("--skip-message", default="")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command after --")

    args = parser.parse_args(list(argv) if argv is not None else None)

    repo_root = _repo_root()
    stage = args.stage
    out_dir = Path(args.out_dir) if args.out_dir else repo_root / "build_output" / stage
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_defaults = {
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
    timeout_sec = args.timeout_sec or timeout_defaults.get(stage, 600)

    assets_payload: Dict[str, Any] = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    assets_src = args.assets_from or str(repo_root / "build_output" / "prepare" / "results.json")
    assets_json, assets_err = _safe_json_load(Path(assets_src))
    if assets_json and isinstance(assets_json.get("assets"), dict):
        # best-effort: keep only required keys
        for k in ("dataset", "model"):
            v = assets_json["assets"].get(k)
            if isinstance(v, dict):
                assets_payload[k] = {
                    "path": str(v.get("path", "")),
                    "source": str(v.get("source", "")),
                    "version": str(v.get("version", "")),
                    "sha256": str(v.get("sha256", "")),
                }

    cmd_tokens = list(args.cmd)
    if cmd_tokens and cmd_tokens[0] == "--":
        cmd_tokens = cmd_tokens[1:]

    needs_python = args.needs_python
    uses_python_placeholder = False
    if cmd_tokens:
        if cmd_tokens[0] in {"python", "python3", "python3.8", "python3.9", "python3.10", "python3.11"}:
            uses_python_placeholder = True
        if any(tok == "{python}" for tok in cmd_tokens):
            uses_python_placeholder = True
    if uses_python_placeholder:
        needs_python = True

    resolution, report_json, py_err = resolve_python(
        cli_python=args.cli_python,
        needs_python=needs_python,
        report_path=args.report_path,
    )

    meta: Dict[str, Any] = {
        "python": resolution.python if resolution else "",
        "python_resolution": {
            "method": resolution.method if resolution else "",
            "report_path": resolution.report_path if resolution else (args.report_path or os.environ.get("SCIMLOPSBENCH_REPORT") or DEFAULT_REPORT_PATH),
            "warning": resolution.warning if resolution else "",
        },
        "git_commit": _git_commit(repo_root) or "",
        "env_vars": {
            k: os.environ.get(k, "")
            for k in [
                "CUDA_VISIBLE_DEVICES",
                "HF_HOME",
                "TRANSFORMERS_CACHE",
                "TORCH_HOME",
                "XDG_CACHE_HOME",
                "WANDB_MODE",
                "WANDB_DIR",
                "TMPDIR",
                "PYTHONPATH",
                "SCIMLOPSBENCH_REPORT",
                "SCIMLOPSBENCH_PYTHON",
                "SCIMLOPSBENCH_AIM_ATTNPROBE_COMPAT",
            ]
            if k in os.environ
        },
        "decision_reason": args.decision_reason,
        "timestamp_utc": _utc_timestamp(),
    }

    command_str = ""
    if cmd_tokens:
        command_str = " ".join(shlex.quote(t) for t in cmd_tokens)

    if args.skip:
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"[runner] stage={stage} skipped\n")
            if args.skip_message:
                f.write(args.skip_message.strip() + "\n")
            if args.decision_reason:
                f.write("\n[decision_reason]\n")
                f.write(args.decision_reason.strip() + "\n")
        payload = {
            "status": "skipped",
            "skip_reason": args.skip_reason,
            "exit_code": 0,
            "stage": stage,
            "task": args.task,
            "command": command_str,
            "timeout_sec": timeout_sec,
            "framework": args.framework,
            "assets": assets_payload,
            "meta": meta,
            "failure_category": "not_applicable",
            "error_excerpt": "",
        }
        _write_json(results_path, payload)
        return 0

    if needs_python and (resolution is None or py_err):
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"[runner] stage={stage} failed before execution\n")
            f.write((py_err or "python resolution failed") + "\n")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": stage,
            "task": args.task,
            "command": command_str,
            "timeout_sec": timeout_sec,
            "framework": args.framework,
            "assets": assets_payload,
            "meta": meta,
            "failure_category": "missing_report",
            "error_excerpt": _tail_lines(log_path),
        }
        _write_json(results_path, payload)
        return 1

    # Apply env overrides
    child_env = os.environ.copy()
    for item in args.env:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        child_env[k] = v

    # Replace python placeholder(s)
    if resolution and cmd_tokens:
        if cmd_tokens[0] in {"python", "python3", "python3.8", "python3.9", "python3.10", "python3.11"}:
            cmd_tokens[0] = resolution.python
        cmd_tokens = [resolution.python if tok == "{python}" else tok for tok in cmd_tokens]
        command_str = " ".join(shlex.quote(t) for t in cmd_tokens)

    spawn_error = False
    timed_out = False
    command_exit_code: Optional[int] = None

    start = time.time()
    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[runner] stage={stage}\n")
        log_f.write(f"[runner] cwd={repo_root}\n")
        log_f.write(f"[runner] timeout_sec={timeout_sec}\n")
        log_f.write(f"[runner] command={command_str}\n")
        if meta["python"]:
            log_f.write(f"[runner] resolved_python={meta['python']} ({meta['python_resolution'].get('method','')})\n")
        if meta["python_resolution"].get("warning"):
            log_f.write(f"[runner] warning={meta['python_resolution']['warning']}\n")
        log_f.write("\n")
        log_f.flush()

        try:
            proc = subprocess.Popen(
                cmd_tokens,
                cwd=str(repo_root),
                env=child_env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError as e:
            spawn_error = True
            log_f.write(f"[runner] spawn error: {e}\n")
        except Exception as e:  # pragma: no cover
            spawn_error = True
            log_f.write(f"[runner] spawn error: {e}\n")
        else:
            try:
                command_exit_code = proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                timed_out = True
                log_f.write(f"\n[runner] timeout after {timeout_sec}s; terminating process\n")
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    command_exit_code = proc.wait(timeout=30)
                except Exception:
                    command_exit_code = None

    duration_sec = max(0.0, time.time() - start)
    log_excerpt = _tail_lines(log_path)

    status = "success"
    stage_exit_code = 0
    failure_category = "unknown"
    if spawn_error:
        status = "failure"
        stage_exit_code = 1
        failure_category = _detect_failure_category(log_excerpt, timed_out=False, spawn_error=True)
    elif timed_out:
        status = "failure"
        stage_exit_code = 1
        failure_category = _detect_failure_category(log_excerpt, timed_out=True, spawn_error=False)
    elif command_exit_code not in (0, None):
        status = "failure"
        stage_exit_code = 1
        failure_category = _detect_failure_category(log_excerpt, timed_out=False, spawn_error=False)

    meta["duration_sec"] = round(duration_sec, 3)
    meta["command_exit_code"] = command_exit_code

    payload = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": stage_exit_code,
        "stage": stage,
        "task": args.task,
        "command": command_str,
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": assets_payload,
        "meta": meta,
        "failure_category": failure_category if status == "failure" else "unknown",
        "error_excerpt": log_excerpt if status == "failure" else "",
    }
    _write_json(results_path, payload)
    return stage_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
