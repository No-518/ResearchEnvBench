#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bench_utils import (
    REPO_ROOT,
    capture_env_vars,
    ensure_dir,
    get_git_commit,
    is_executable_file,
    python_version_string,
    read_json,
    tail_lines,
    utc_timestamp,
    write_json,
)


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


def _report_path(args: argparse.Namespace) -> Path:
    if args.report_path:
        return Path(args.report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path("/opt/scimlopsbench/report.json")


def _resolve_python(args: argparse.Namespace, *, requires_python: bool) -> Tuple[Optional[str], List[str], Optional[str]]:
    """
    Returns (python_executable, warnings, failure_category_if_any).
    """
    warnings: List[str] = []
    if args.python:
        return args.python, warnings, None
    env_py = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_py:
        return env_py, warnings, None

    report_path = _report_path(args)
    try:
        report = read_json(report_path)
        py = report.get("python_path") if isinstance(report, dict) else None
        if isinstance(py, str) and py:
            return py, warnings, None
        warnings.append("Report missing `python_path`; falling back to `python` from PATH.")
        return "python", warnings, None
    except FileNotFoundError:
        if requires_python:
            return None, warnings, "missing_report"
    except Exception:
        if requires_python:
            return None, warnings, "missing_report"

    # Last resort fallback: python from PATH.
    return "python", warnings, None


def _stringify_cmd(cmd: List[str]) -> str:
    try:
        return shlex.join(cmd)
    except Exception:
        return " ".join(cmd)


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


def _write_results(
    *,
    stage: str,
    task: str,
    status: str,
    skip_reason: str,
    exit_code: int,
    timeout_sec: int,
    command: str,
    framework: str,
    assets: Dict[str, Any],
    meta: Dict[str, Any],
    failure_category: str,
    error_excerpt: str,
    out_dir: Path,
) -> None:
    write_json(
        out_dir / "results.json",
        {
            "status": status,
            "skip_reason": skip_reason,
            "exit_code": exit_code,
            "stage": stage,
            "task": task,
            "command": command,
            "timeout_sec": timeout_sec,
            "framework": framework,
            "assets": assets,
            "meta": meta,
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        },
    )


def _load_assets_from_manifest(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}}
    try:
        manifest = read_json(Path(path))
        dataset = manifest.get("dataset", {}) if isinstance(manifest, dict) else {}
        model = manifest.get("model", {}) if isinstance(manifest, dict) else {}
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
        return {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark command runner (logs + results.json).")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    parser.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    parser.add_argument("--timeout-sec", type=int, default=0, help="Override timeout; 0 uses stage defaults.")
    parser.add_argument("--out-dir", default="", help="Default: build_output/<stage>")
    parser.add_argument("--assets-manifest", default="", help="Path to benchmark_assets/manifest.json")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--python", default="", help="Explicit python executable (highest priority).")
    parser.add_argument("--report-path", default="", help="Override agent report path.")
    parser.add_argument(
        "--use-resolved-python",
        action="store_true",
        help="Prefix the executed command with the resolved python interpreter.",
    )
    parser.add_argument("--", dest="double_dash", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command (after --).")

    args = parser.parse_args()

    # Strip leading '--' if present in argparse remainder.
    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    stage = args.stage
    out_dir = Path(args.out_dir) if args.out_dir else (REPO_ROOT / "build_output" / stage)
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"

    timeout_sec = args.timeout_sec if args.timeout_sec > 0 else DEFAULT_TIMEOUTS_SEC.get(stage, 600)

    assets = _load_assets_from_manifest(args.assets_manifest)

    env_keys = [
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "HF_DATASETS_CACHE",
        "TRANSFORMERS_CACHE",
        "XDG_CACHE_HOME",
        "TOKENIZERS_PARALLELISM",
        "PYTHONPATH",
    ]

    meta: Dict[str, Any] = {
        "timestamp_utc": utc_timestamp(),
        "git_commit": get_git_commit(REPO_ROOT),
        "env_vars": capture_env_vars(env_keys),
        "decision_reason": args.decision_reason,
        "warnings": [],
        "python": "",
    }

    requires_python = bool(args.use_resolved_python)
    python_exe, warnings, failure_category = _resolve_python(args, requires_python=requires_python)
    if warnings:
        meta["warnings"].extend(warnings)

    if failure_category == "missing_report":
        write_text = f"missing/invalid report for python resolution (looked at: {_report_path(args)})\n"
        log_path.write_text(write_text, encoding="utf-8")
        meta["python"] = sys.executable
        _write_results(
            stage=stage,
            task=args.task,
            status="failure",
            skip_reason="unknown",
            exit_code=1,
            timeout_sec=timeout_sec,
            command="",
            framework=args.framework,
            assets=assets,
            meta=meta,
            failure_category="missing_report",
            error_excerpt=write_text.strip(),
            out_dir=out_dir,
        )
        return 1

    if requires_python:
        if not python_exe:
            log_path.write_text("python resolution failed\n", encoding="utf-8")
            _write_results(
                stage=stage,
                task=args.task,
                status="failure",
                skip_reason="unknown",
                exit_code=1,
                timeout_sec=timeout_sec,
                command="",
                framework=args.framework,
                assets=assets,
                meta=meta,
                failure_category="missing_report",
                error_excerpt="python resolution failed",
                out_dir=out_dir,
            )
            return 1
        meta["python_executable"] = python_exe
        py_ver = python_version_string(python_exe)
        meta["python"] = py_ver if py_ver else ""
        try:
            if is_executable_file(Path(python_exe)):
                meta["python_ok"] = True
            else:
                meta["python_ok"] = True  # could still be resolvable via PATH (e.g. "python")
        except Exception:
            meta["python_ok"] = False
    else:
        meta["python_executable"] = python_exe or ""
        meta["python"] = python_version_string(python_exe) if python_exe else ""

    if not cmd:
        log_path.write_text("no command provided\n", encoding="utf-8")
        _write_results(
            stage=stage,
            task=args.task,
            status="failure",
            skip_reason="unknown",
            exit_code=1,
            timeout_sec=timeout_sec,
            command="",
            framework=args.framework,
            assets=assets,
            meta=meta,
            failure_category="args_unknown",
            error_excerpt="no command provided",
            out_dir=out_dir,
        )
        return 1

    actual_cmd = ([python_exe] + cmd) if args.use_resolved_python else cmd
    cmd_str = _stringify_cmd([c for c in actual_cmd if c is not None])

    failure_category_out = "unknown"
    status = "success"
    exit_code = 0
    proc_rc: Optional[int] = None

    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log_f:
            proc = subprocess.Popen(actual_cmd, cwd=REPO_ROOT, stdout=log_f, stderr=subprocess.STDOUT, env=os.environ.copy())
            try:
                proc.wait(timeout=timeout_sec)
                proc_rc = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=30)
                proc_rc = 124
                status = "failure"
                exit_code = 1
                failure_category_out = "timeout"
    except FileNotFoundError as e:
        status = "failure"
        exit_code = 1
        failure_category_out = "entrypoint_not_found"
        log_path.write_text(f"FileNotFoundError: {e}\n", encoding="utf-8")
    except Exception as e:
        status = "failure"
        exit_code = 1
        failure_category_out = "runtime"
        log_path.write_text(f"{type(e).__name__}: {e}\n", encoding="utf-8")

    if status != "failure":
        if proc_rc is None:
            status = "failure"
            exit_code = 1
            failure_category_out = "unknown"
        elif proc_rc != 0:
            status = "failure"
            exit_code = 1
            failure_category_out = "runtime"

    meta["command_exit_code"] = proc_rc

    error_excerpt = tail_lines(log_path, max_lines=220)
    if (
        status == "failure"
        and stage in ("cpu", "single_gpu", "multi_gpu")
        and failure_category_out in ("unknown", "runtime")
    ):
        inferred = _infer_failure_category_from_log(error_excerpt)
        if inferred:
            failure_category_out = inferred

    _write_results(
        stage=stage,
        task=args.task,
        status=status,
        skip_reason="not_applicable" if status != "skipped" else "unknown",
        exit_code=exit_code,
        timeout_sec=timeout_sec,
        command=cmd_str,
        framework=args.framework,
        assets=assets,
        meta=meta,
        failure_category=failure_category_out if status == "failure" else "not_applicable",
        error_excerpt=error_excerpt,
        out_dir=out_dir,
    )

    return 0 if status in ("success", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
