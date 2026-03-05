#!/usr/bin/env python3
"""
Unified executor for benchmark stages.

This script is intentionally self-contained (stdlib only). It is designed to:
- Resolve the "target" Python interpreter from the agent report or CLI/env overrides.
- Run a command with timeout, capturing stdout/stderr to build_output/<stage>/log.txt.
- Always write build_output/<stage>/results.json (even on failure).
"""

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
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"

DEFAULT_TIMEOUTS_SEC = {
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json: {path}: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"failed reading json: {path}: {e}"


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        return out
    except Exception:  # noqa: BLE001
        return ""


def _tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-max_lines:]
        return "\n".join(tail)
    except FileNotFoundError:
        return ""
    except Exception as e:  # noqa: BLE001
        return f"(failed to read log tail: {e})"


def _infer_failure_category_from_log(log_text: str) -> str:
    hay = (log_text or "").lower()
    if not hay:
        return ""
    if "modulenotfounderror" in hay or "no module named" in hay:
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
    if "torchrun: command not found" in hay or "command not found: torchrun" in hay:
        return "deps"
    if "does not seem to have any of the loading methods defined" in hay and "placeholder" in hay:
        return "deps"
    return ""


def _selected_env_vars() -> Dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_DATASETS_CACHE",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "WANDB_DISABLED",
        "WANDB_MODE",
        "WANDB_PROJECT",
        "TOKENIZERS_PARALLELISM",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
    ]
    out: Dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            out[k] = v
    return out


@dataclass
class PythonResolution:
    python: str
    report_path: str
    from_source: str  # cli|env|report|path
    warning: str = ""


def resolve_report_path(cli_report_path: Optional[str]) -> str:
    if cli_report_path:
        return cli_report_path
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return env_path
    return DEFAULT_REPORT_PATH


def resolve_python(
    *,
    cli_python: Optional[str],
    cli_report_path: Optional[str],
    require_report_when_no_cli_python: bool,
) -> Tuple[Optional[PythonResolution], Optional[Tuple[str, str]]]:
    """
    Returns (PythonResolution|None, failure=(failure_category, message)|None).
    """
    report_path = resolve_report_path(cli_report_path)

    if cli_python:
        return PythonResolution(
            python=cli_python,
            report_path=report_path,
            from_source="cli",
        ), None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return PythonResolution(
            python=env_python,
            report_path=report_path,
            from_source="env",
        ), None

    report_file = Path(report_path)
    report, err = _safe_read_json(report_file)
    if report is None:
        if require_report_when_no_cli_python:
            return None, ("missing_report", err or "missing report")
        py = shutil.which("python3") or shutil.which("python") or "python"
        return PythonResolution(
            python=py,
            report_path=report_path,
            from_source="path",
            warning=f"report unavailable; falling back to {py}",
        ), None

    python_path = report.get("python_path")
    if not python_path:
        py = shutil.which("python3") or shutil.which("python") or "python"
        return PythonResolution(
            python=py,
            report_path=report_path,
            from_source="path",
            warning="report missing python_path; falling back to PATH python",
        ), None

    return PythonResolution(
        python=str(python_path),
        report_path=report_path,
        from_source="report",
    ), None


def _is_executable(path: str) -> bool:
    p = Path(path)
    return p.exists() and os.access(str(p), os.X_OK) and p.is_file()


def _format_cmd(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def run_with_timeout(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    timeout_sec: int,
    log_path: Path,
) -> Tuple[int, bool]:
    """
    Returns (returncode, timed_out).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as logf:
        logf.write(f"[runner] timestamp_utc={_utc_timestamp()}\n")
        logf.write(f"[runner] cwd={cwd}\n")
        logf.write(f"[runner] command={_format_cmd(cmd)}\n")
        logf.flush()

        proc = subprocess.Popen(
            list(cmd),
            cwd=str(cwd),
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid if os.name != "nt" else None,
            text=True,
        )
        timed_out = False
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                try:
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except Exception:
                    pass
        return int(proc.returncode or 0), timed_out


def _load_assets(repo_root: Path) -> Dict[str, Dict[str, str]]:
    manifest_path = repo_root / "benchmark_assets" / "manifest.json"
    data, _err = _safe_read_json(manifest_path)
    if not isinstance(data, dict):
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    dataset = data.get("dataset") if isinstance(data.get("dataset"), dict) else {}
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
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


def write_results(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark stage runner")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--timeout-sec", type=int, default=0)
    parser.add_argument("--framework", default="unknown")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--skip", action="store_true")
    parser.add_argument(
        "--skip-reason",
        default="unknown",
        choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"],
    )
    parser.add_argument("--failure-category", default="unknown")
    parser.add_argument("--python", default="")
    parser.add_argument("--report-path", default="")
    parser.add_argument(
        "--requires-python",
        action="store_true",
        help="If set, fail when report missing/invalid and --python not provided",
    )
    parser.add_argument(
        "--use-python",
        action="store_true",
        help="Prefix the command with the resolved python executable",
    )
    parser.add_argument(
        "--print-resolved-python",
        action="store_true",
        help="Print the resolved python executable and exit",
    )
    parser.add_argument("--cwd", default="")
    parser.add_argument("--env", action="append", default=[], help="Extra env VAR=VALUE")
    parser.add_argument("--", dest="cmd_sep", action="store_true")  # dummy for help formatting
    parser.add_argument("cmd", nargs=argparse.REMAINDER)

    args = parser.parse_args(list(argv) if argv is not None else None)

    repo_root = _repo_root()
    stage = args.stage
    task = args.task

    out_dir = Path(args.out_dir) if args.out_dir else Path("build_output") / stage
    out_dir = (repo_root / out_dir).resolve()
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = args.timeout_sec or int(DEFAULT_TIMEOUTS_SEC.get(stage, 600))

    py_res, py_fail = resolve_python(
        cli_python=args.python or None,
        cli_report_path=args.report_path or None,
        require_report_when_no_cli_python=bool(args.requires_python),
    )

    if args.print_resolved_python:
        if py_res is None:
            sys.stderr.write((py_fail[1] if py_fail else "failed to resolve python") + "\n")
            return 1
        print(py_res.python)
        return 0

    assets = _load_assets(repo_root)
    meta: Dict[str, Any] = {
        "python": py_res.python if py_res else "",
        "git_commit": _git_commit(repo_root),
        "env_vars": _selected_env_vars(),
        "decision_reason": args.decision_reason,
        "timestamp_utc": _utc_timestamp(),
    }
    if py_res and py_res.warning:
        meta["warning"] = py_res.warning
    if py_res and py_res.from_source:
        meta["python_resolution"] = py_res.from_source
        meta["report_path"] = py_res.report_path

    if py_res is None and args.requires_python:
        failure_category, msg = py_fail if py_fail else ("missing_report", "missing report")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": stage,
            "task": task,
            "command": "",
            "timeout_sec": timeout_sec,
            "framework": args.framework,
            "assets": assets,
            "meta": meta,
            "failure_category": failure_category,
            "error_excerpt": msg,
        }
        write_results(results_path, payload)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(msg + "\n", encoding="utf-8")
        return 1

    if args.skip:
        payload = {
            "status": "skipped",
            "skip_reason": args.skip_reason,
            "exit_code": 0,
            "stage": stage,
            "task": task,
            "command": _format_cmd(args.cmd) if args.cmd else "",
            "timeout_sec": timeout_sec,
            "framework": args.framework,
            "assets": assets,
            "meta": meta,
            "failure_category": "not_applicable",
            "error_excerpt": "",
        }
        write_results(results_path, payload)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"[runner] skipped stage={stage} reason={args.skip_reason}\n",
            encoding="utf-8",
        )
        return 0

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": stage,
            "task": task,
            "command": "",
            "timeout_sec": timeout_sec,
            "framework": args.framework,
            "assets": assets,
            "meta": meta,
            "failure_category": "args_unknown",
            "error_excerpt": "no command provided",
        }
        write_results(results_path, payload)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("no command provided\n", encoding="utf-8")
        return 1

    if args.use_python:
        if py_res is None:
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": stage,
                "task": task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": args.framework,
                "assets": assets,
                "meta": meta,
                "failure_category": "missing_report",
                "error_excerpt": "python resolution failed",
            }
            write_results(results_path, payload)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("python resolution failed\n", encoding="utf-8")
            return 1

        python_exe = py_res.python
        if Path(python_exe).is_absolute() and not _is_executable(python_exe):
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": stage,
                "task": task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": args.framework,
                "assets": assets,
                "meta": meta,
                "failure_category": "missing_report",
                "error_excerpt": f"python_path is not executable: {python_exe}",
            }
            write_results(results_path, payload)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"python_path is not executable: {python_exe}\n", encoding="utf-8")
            return 1
        cmd = [python_exe] + cmd

    env = os.environ.copy()
    for kv in args.env:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        env[k] = v

    cwd = Path(args.cwd).resolve() if args.cwd else repo_root
    returncode, timed_out = run_with_timeout(
        cmd,
        cwd=cwd,
        env=env,
        timeout_sec=timeout_sec,
        log_path=log_path,
    )

    status = "success" if returncode == 0 and not timed_out else "failure"
    exit_code = 0 if status != "failure" else 1

    failure_category = args.failure_category
    if status == "failure":
        if timed_out:
            failure_category = "timeout"
        elif returncode == 127:
            failure_category = "entrypoint_not_found"
        elif failure_category == "unknown":
            failure_category = "runtime"

    error_excerpt = _tail_text(log_path, max_lines=220) if status == "failure" else ""
    if (
        status == "failure"
        and stage in ("cpu", "single_gpu", "multi_gpu")
        and failure_category in ("unknown", "runtime")
    ):
        inferred = _infer_failure_category_from_log(error_excerpt)
        if inferred:
            failure_category = inferred

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": stage,
        "task": task,
        "command": _format_cmd(cmd),
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": assets,
        "meta": meta,
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }
    if timed_out:
        payload["meta"]["timed_out"] = True

    write_results(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
