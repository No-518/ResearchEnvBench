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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPORT_PATH_DEFAULT = "/opt/scimlopsbench/report.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_root() -> Path:
    # benchmark_scripts/runner.py -> repo root is parent of benchmark_scripts
    return Path(__file__).resolve().parent.parent


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tail_lines(path: Path, max_lines: int = 220, max_bytes: int = 128 * 1024) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
    except Exception:
        try:
            text = _read_text(path)
        except Exception:
            return ""
    lines = text.splitlines()
    excerpt = "\n".join(lines[-max_lines:])
    return excerpt


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
        return out
    except Exception:
        return ""


def _is_executable_file(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(str(path), os.X_OK)


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(REPORT_PATH_DEFAULT)


def _load_report_json(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not report_path.exists():
        return None, f"report.json not found at {report_path}"
    try:
        data = json.loads(_read_text(report_path))
    except Exception as e:
        return None, f"failed to parse report.json ({report_path}): {e}"
    if not isinstance(data, dict):
        return None, f"report.json root is not an object ({report_path})"
    return data, None


@dataclass(frozen=True)
class PythonResolution:
    python: Optional[str]
    source: str
    warning: str = ""
    reported_python_path: str = ""
    report_path: str = ""


def _resolve_python(
    cli_python: Optional[str],
    report_path: Path,
    requires_python: bool,
) -> Tuple[PythonResolution, Optional[str]]:
    if cli_python:
        return PythonResolution(
            python=cli_python,
            source="cli",
            report_path=str(report_path),
        ), None

    if os.environ.get("SCIMLOPSBENCH_PYTHON"):
        return PythonResolution(
            python=os.environ["SCIMLOPSBENCH_PYTHON"],
            source="env",
            report_path=str(report_path),
        ), None

    report, err = _load_report_json(report_path)
    if err is not None:
        if requires_python:
            return PythonResolution(
                python=None,
                source="report",
                warning="",
                report_path=str(report_path),
            ), err
        # For non-python stages we can proceed without the report.
        return PythonResolution(
            python=None,
            source="report",
            warning="report missing/invalid but stage does not require python",
            report_path=str(report_path),
        ), None

    reported_python_path = str(report.get("python_path", "") or "")
    if reported_python_path:
        return PythonResolution(
            python=reported_python_path,
            source="report",
            reported_python_path=reported_python_path,
            report_path=str(report_path),
        ), None

    if requires_python:
        return PythonResolution(
            python=None,
            source="report",
            reported_python_path="",
            report_path=str(report_path),
        ), f"report.json missing required key: python_path ({report_path})"

    return PythonResolution(
        python=None,
        source="report",
        warning="report.json missing python_path but stage does not require python",
        report_path=str(report_path),
    ), None


def _python_probe(python: str, timeout_sec: int = 20) -> Tuple[bool, str, str]:
    try:
        exe = subprocess.check_output(
            [python, "-c", "import sys; print(sys.executable)"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
        ).strip()
    except Exception as e:
        return False, "", f"failed to execute python probe: {e}"

    try:
        ver = subprocess.check_output(
            [python, "-c", "import platform; print(platform.python_version())"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
        ).strip()
    except Exception as e:
        return True, exe, f"python version probe failed: {e}"
    return True, exe, ver


def _find_fallback_python() -> str:
    return shutil.which("python3") or shutil.which("python") or ""


def _maybe_load_assets_from_prepare(prepare_results_path: Optional[str]) -> Dict[str, Any]:
    if not prepare_results_path:
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    path = Path(prepare_results_path)
    if not path.exists():
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    try:
        data = json.loads(_read_text(path))
        assets = data.get("assets", {})
        if isinstance(assets, dict) and "dataset" in assets and "model" in assets:
            return assets
    except Exception:
        pass
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _classify_failure(exit_code: int, log_excerpt: str) -> str:
    raw_lines = (log_excerpt or "").splitlines()
    # Filter out runner metadata lines to avoid false matches (e.g., "timeout_sec=").
    filtered = "\n".join([ln for ln in raw_lines if not ln.startswith("[runner]")])
    text = filtered.lower()
    if "no such file or directory" in text or "not found" in text and "python" in text:
        return "entrypoint_not_found"
    if "permission denied" in text:
        return "deps"
    if "out of memory" in text or "cuda out of memory" in text:
        return "oom"
    if "timed out" in text or "timeoutexpired" in text or "timeout expired" in text:
        return "timeout"
    if exit_code == 127:
        return "entrypoint_not_found"
    return "runtime"


def _parse_env_kv(pairs: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        out[k] = v
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark command runner.")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--timeout-sec", type=int, default=600)
    parser.add_argument("--framework", default="unknown")
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python", dest="python_override", default=None)
    parser.add_argument("--no-requires-python", action="store_true")
    parser.add_argument("--prepare-results", default=None, help="Path to build_output/prepare/results.json to copy assets from.")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--env", action="append", default=[], help="Extra env vars (KEY=VAL).")
    parser.add_argument("--skip", action="store_true")
    parser.add_argument("--skip-reason", default="unknown", choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"])
    parser.add_argument("--command-display", default="", help="Override the command string recorded in results.json.")
    parser.add_argument("--failure-category-on-fail", default="unknown")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run, after '--'. Use '{{PYTHON}}' placeholder to inject resolved python.")

    args = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = (repo_root / args.out_dir).resolve()
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    requires_python = not args.no_requires_python
    report_path = _resolve_report_path(args.report_path)

    base_results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": args.stage,
        "task": args.task,
        "command": "",
        "timeout_sec": int(args.timeout_sec),
        "framework": args.framework,
        "assets": _maybe_load_assets_from_prepare(args.prepare_results),
        "meta": {
            "python": "",
            "git_commit": _git_commit(repo_root),
            "env_vars": {},
            "decision_reason": args.decision_reason,
            "timestamp_utc": _utc_now_iso(),
            "python_resolution": {},
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    extra_env = _parse_env_kv(args.env)
    interesting_env_keys = [
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_DATASETS_CACHE",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "XDG_CACHE_HOME",
        "TOKENIZERS_PARALLELISM",
        "PYTORCH_CUDA_ALLOC_CONF",
    ]
    base_results["meta"]["env_vars"] = {k: os.environ.get(k, "") for k in interesting_env_keys}
    base_results["meta"]["env_vars"].update(extra_env)

    if args.skip:
        base_results["status"] = "skipped"
        base_results["skip_reason"] = args.skip_reason
        base_results["exit_code"] = 0
        base_results["command"] = args.command_display or "(skipped)"
        base_results["failure_category"] = "unknown"
        base_results["error_excerpt"] = ""
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"[runner] stage skipped: {args.stage}\n")
            f.write(f"[runner] skip_reason: {args.skip_reason}\n")
            f.write(f"[runner] timestamp_utc: {_utc_now_iso()}\n")
        _write_json(results_path, base_results)
        return 0

    resolution, res_err = _resolve_python(
        cli_python=args.python_override,
        report_path=report_path,
        requires_python=requires_python,
    )
    base_results["meta"]["python_resolution"] = {
        "source": resolution.source,
        "warning": resolution.warning,
        "report_path": resolution.report_path,
        "reported_python_path": resolution.reported_python_path,
        "cli_python": args.python_override or "",
        "env_SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
    }

    python_for_placeholder: Optional[str] = None
    if requires_python:
        if res_err is not None:
            base_results["status"] = "failure"
            base_results["exit_code"] = 1
            base_results["failure_category"] = "missing_report"
            base_results["command"] = args.command_display or ""
            with log_path.open("w", encoding="utf-8") as f:
                f.write(f"[runner] failed to resolve python for stage={args.stage}\n")
                f.write(f"[runner] {res_err}\n")
            base_results["error_excerpt"] = _tail_lines(log_path)
            _write_json(results_path, base_results)
            return 1
        if not resolution.python:
            base_results["status"] = "failure"
            base_results["exit_code"] = 1
            base_results["failure_category"] = "missing_report"
            base_results["command"] = args.command_display or ""
            with log_path.open("w", encoding="utf-8") as f:
                f.write(f"[runner] python resolution produced empty python path for stage={args.stage}\n")
            base_results["error_excerpt"] = _tail_lines(log_path)
            _write_json(results_path, base_results)
            return 1
        python_for_placeholder = resolution.python

        ok, exe, ver_or_err = _python_probe(python_for_placeholder)
        base_results["meta"]["python"] = python_for_placeholder
        base_results["meta"]["python_executable"] = exe
        base_results["meta"]["python_version"] = ver_or_err if ok else ""
        if not ok:
            # Last-resort fallback: use python from PATH, but record a warning.
            fallback = _find_fallback_python()
            if fallback:
                ok2, exe2, ver2 = _python_probe(fallback)
                if ok2:
                    base_results["meta"]["python_resolution"]["warning"] = (
                        f"python probe failed for resolved python ({python_for_placeholder}); using fallback from PATH: {fallback}"
                    )
                    python_for_placeholder = fallback
                    base_results["meta"]["python"] = fallback
                    base_results["meta"]["python_executable"] = exe2
                    base_results["meta"]["python_version"] = ver2
                else:
                    base_results["status"] = "failure"
                    base_results["exit_code"] = 1
                    base_results["failure_category"] = "path_hallucination"
                    base_results["command"] = args.command_display or ""
                    with log_path.open("w", encoding="utf-8") as f:
                        f.write(f"[runner] python probe failed for {python_for_placeholder}\n")
                        f.write(f"[runner] {ver_or_err}\n")
                        f.write(f"[runner] fallback python probe also failed for {fallback}\n")
                        f.write(f"[runner] {ver2}\n")
                    base_results["error_excerpt"] = _tail_lines(log_path)
                    _write_json(results_path, base_results)
                    return 1
            else:
                base_results["status"] = "failure"
                base_results["exit_code"] = 1
                base_results["failure_category"] = "path_hallucination"
                base_results["command"] = args.command_display or ""
                with log_path.open("w", encoding="utf-8") as f:
                    f.write(f"[runner] python probe failed for {python_for_placeholder}\n")
                    f.write(f"[runner] {ver_or_err}\n")
                    f.write("[runner] no fallback python found on PATH\n")
                base_results["error_excerpt"] = _tail_lines(log_path)
                _write_json(results_path, base_results)
                return 1
    else:
        base_results["meta"]["python"] = resolution.python or ""

    # Build command from args.cmd (strip leading '--' if present).
    cmd = list(args.cmd)
    while cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if not cmd:
        base_results["status"] = "failure"
        base_results["exit_code"] = 1
        base_results["failure_category"] = "args_unknown"
        base_results["command"] = args.command_display or ""
        with log_path.open("w", encoding="utf-8") as f:
            f.write("[runner] no command provided (use `-- <cmd...>`)\n")
        base_results["error_excerpt"] = _tail_lines(log_path)
        _write_json(results_path, base_results)
        return 1

    if python_for_placeholder:
        cmd = [python_for_placeholder if part in ("{{PYTHON}}", "{PYTHON}", "{python}", "{{python}}") else part for part in cmd]

    command_display = args.command_display or " ".join(shlex.quote(c) for c in cmd)
    base_results["command"] = command_display

    env = os.environ.copy()
    env.update(extra_env)

    # Execute with process-group handling so we can terminate children on timeout.
    start = time.time()
    exit_code = 1
    timed_out = False
    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[runner] stage={args.stage} task={args.task}\n")
        log_f.write(f"[runner] cwd={repo_root}\n")
        log_f.write(f"[runner] command={command_display}\n")
        log_f.write(f"[runner] timeout_sec={args.timeout_sec}\n")
        log_f.write(f"[runner] timestamp_utc_start={_utc_now_iso()}\n")
        log_f.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=args.timeout_sec)
            exit_code = int(proc.returncode or 0)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(3)
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            exit_code = 124
        finally:
            duration = time.time() - start
            log_f.write(f"\n[runner] timestamp_utc_end={_utc_now_iso()}\n")
            log_f.write(f"[runner] duration_sec={duration:.2f}\n")
            log_f.write(f"[runner] exit_code={exit_code}\n")

    base_results["exit_code"] = 0 if exit_code == 0 else 1
    if exit_code == 0:
        base_results["status"] = "success"
        base_results["skip_reason"] = "not_applicable"
        base_results["failure_category"] = "unknown"
    else:
        base_results["status"] = "failure"
        base_results["skip_reason"] = "not_applicable"
        excerpt = _tail_lines(log_path)
        base_results["error_excerpt"] = excerpt
        if timed_out:
            base_results["failure_category"] = "timeout"
        elif args.failure_category_on_fail != "unknown":
            base_results["failure_category"] = args.failure_category_on_fail
        else:
            base_results["failure_category"] = _classify_failure(exit_code, excerpt)

    if not base_results["error_excerpt"]:
        base_results["error_excerpt"] = _tail_lines(log_path)

    _write_json(results_path, base_results)
    return 0 if base_results["status"] in ("success", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
