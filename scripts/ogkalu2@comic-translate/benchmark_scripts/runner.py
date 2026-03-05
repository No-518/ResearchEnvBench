#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text(path: Path, content: str) -> None:
    _safe_mkdir(path.parent)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    _safe_mkdir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except Exception:
        return ""


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            text=True,
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
    return DEFAULT_REPORT_PATH


def _load_report(report_path: Path) -> Tuple[Optional[dict], Optional[str]]:
    if not report_path.exists():
        return None, f"Report not found: {report_path}"
    try:
        data = json.loads(_read_text(report_path))
        if not isinstance(data, dict):
            return None, f"Report JSON root is not an object: {report_path}"
        return data, None
    except Exception as e:
        return None, f"Failed to parse report JSON: {report_path}: {e}"


def _is_executable_file(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


@dataclass
class PythonResolution:
    python_path: Optional[str]
    source: str
    warning: str


def resolve_python_interpreter(
    *,
    cli_python: Optional[str],
    requires_python: bool,
    report_path: Path,
) -> Tuple[Optional[PythonResolution], Optional[str]]:
    if cli_python:
        p = Path(cli_python)
        if not _is_executable_file(p):
            return None, f"--python points to non-executable file: {p}"
        return PythonResolution(str(p), "cli", ""), None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        p = Path(env_python)
        if not _is_executable_file(p):
            return None, f"SCIMLOPSBENCH_PYTHON points to non-executable file: {p}"
        return PythonResolution(str(p), "env:SCIMLOPSBENCH_PYTHON", ""), None

    report, report_err = _load_report(report_path)
    if report is None:
        if requires_python:
            return None, report_err or "missing_report"
        return PythonResolution(None, "none", ""), None

    python_path = report.get("python_path")
    if isinstance(python_path, str) and python_path.strip():
        p = Path(python_path)
        if _is_executable_file(p):
            return PythonResolution(str(p), "report:python_path", ""), None
        # Fallback to PATH python, but record warning.
        fallback = shutil.which("python") or shutil.which("python3")
        if fallback:
            warn = f"report.python_path is not executable; falling back to PATH python: {fallback}"
            return PythonResolution(str(Path(fallback)), "path_fallback", warn), None
        if requires_python:
            return None, f"report.python_path is not executable and no python found in PATH: {p}"
        return PythonResolution(None, "none", ""), None

    # python_path missing in report
    fallback = shutil.which("python") or shutil.which("python3")
    if fallback:
        warn = f"report missing python_path; falling back to PATH python: {fallback}"
        return PythonResolution(str(Path(fallback)), "path_fallback", warn), None
    if requires_python:
        return None, "report missing python_path and no python found in PATH"
    return PythonResolution(None, "none", ""), None


def _default_env(repo_root: Path) -> Dict[str, str]:
    cache_root = repo_root / "benchmark_assets" / "cache"
    env = {}

    # Keep common caches within the repo (required: scripts must write only under new dirs).
    env["HF_HOME"] = str(cache_root / "hf_home")
    env["HUGGINGFACE_HUB_CACHE"] = str(cache_root / "huggingface_hub")
    env["HF_HUB_CACHE"] = str(cache_root / "huggingface_hub")
    env["TRANSFORMERS_CACHE"] = str(cache_root / "transformers")
    env["HF_DATASETS_CACHE"] = str(cache_root / "datasets")
    env["TORCH_HOME"] = str(cache_root / "torch")
    env["XDG_CACHE_HOME"] = str(cache_root / "xdg_cache")
    env["XDG_CONFIG_HOME"] = str(cache_root / "xdg_config")
    env["XDG_DATA_HOME"] = str(cache_root / "xdg_data")
    return env


def _parse_kv_list(items: List[str]) -> Tuple[Dict[str, str], Optional[str]]:
    env: Dict[str, str] = {}
    for item in items:
        if "=" not in item:
            return {}, f"Invalid --env item (expected KEY=VAL): {item}"
        k, v = item.split("=", 1)
        k = k.strip()
        if not k:
            return {}, f"Invalid --env item (empty key): {item}"
        env[k] = v
    return env, None


def _stringify_command(argv: List[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def _infer_framework(command_argv: List[str]) -> str:
    cmd = " ".join(command_argv)
    low = cmd.lower()
    if "torch" in low or "torchrun" in low:
        return "pytorch"
    if "tensorflow" in low or "tf." in low:
        return "tensorflow"
    if "jax" in low:
        return "jax"
    if "onnxruntime" in low or "onnx" in low:
        return "unknown"
    return "unknown"


def _normalize_failure_category(cat: Optional[str]) -> str:
    if not cat:
        return "unknown"
    return cat


def run_command(
    *,
    stage: str,
    task: str,
    command_argv: List[str],
    timeout_sec: int,
    out_dir: Path,
    requires_python: bool,
    report_path: Path,
    cli_python: Optional[str],
    framework: str,
    decision_reason: str,
    skip: bool,
    skip_reason: str,
    failure_category: Optional[str],
    extra_assets: Optional[dict],
    extra_meta: Optional[dict],
    extra_env: Dict[str, str],
) -> int:
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    start_utc = _utc_timestamp()

    py_res, py_err = resolve_python_interpreter(
        cli_python=cli_python,
        requires_python=requires_python,
        report_path=report_path,
    )

    # Pre-populate minimal result structure; always written.
    result: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": skip_reason or "unknown",
        "exit_code": 1,
        "stage": stage,
        "task": task,
        "command": _stringify_command(command_argv) if command_argv else "",
        "timeout_sec": int(timeout_sec),
        "framework": framework if framework != "auto" else _infer_framework(command_argv),
        "assets": extra_assets
        or {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": (py_res.python_path if py_res else ""),
            "python_resolution_source": (py_res.source if py_res else ""),
            "python_resolution_warning": (py_res.warning if py_res else ""),
            "git_commit": _git_commit(REPO_ROOT),
            "env_vars": {},
            "decision_reason": decision_reason,
            "timestamp_utc": start_utc,
        },
        "failure_category": _normalize_failure_category(failure_category),
        "error_excerpt": "",
    }

    # Capture key env vars for debugging; do not dump full env.
    env_interest = [
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "HF_DATASETS_CACHE",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
    ]
    env_snapshot = {k: os.environ.get(k, "") for k in env_interest if k in os.environ}
    result["meta"]["env_vars"] = env_snapshot

    if extra_meta:
        # Avoid overwriting required keys; shallow merge.
        for k, v in extra_meta.items():
            if k in result["meta"] and isinstance(result["meta"][k], dict) and isinstance(v, dict):
                result["meta"][k].update(v)
            else:
                result["meta"][k] = v

    # If python is required and we couldn't resolve it, fail fast.
    if requires_python and (py_res is None):
        _write_text(log_path, (py_err or "missing_report") + "\n")
        result["failure_category"] = "missing_report"
        result["error_excerpt"] = _tail_lines(log_path, 80)
        _write_json(results_path, result)
        return 1

    # Skip short-circuit.
    if skip:
        _write_text(log_path, f"SKIPPED: {skip_reason}\n")
        result["status"] = "skipped"
        result["exit_code"] = 0
        result["failure_category"] = "unknown"
        result["error_excerpt"] = ""
        _write_json(results_path, result)
        return 0

    # Execute
    env = os.environ.copy()
    env.update(_default_env(REPO_ROOT))
    env.update(extra_env)

    # Write a header to log for reproducibility.
    header = textwrap.dedent(
        f"""\
        stage={stage}
        task={task}
        start_utc={start_utc}
        cwd={REPO_ROOT}
        command={_stringify_command(command_argv)}
        timeout_sec={timeout_sec}
        """
    )
    _write_text(log_path, header)

    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log_f:
            proc = subprocess.Popen(
                command_argv,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                rc = proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=30)
                except Exception:
                    pass
                result["status"] = "failure"
                result["exit_code"] = 1
                result["failure_category"] = "timeout"
                result["error_excerpt"] = _tail_lines(log_path, 240)
                _write_json(results_path, result)
                return 1
    except FileNotFoundError as e:
        with log_path.open("a", encoding="utf-8", errors="replace") as log_f:
            log_f.write(f"\nFileNotFoundError: {e}\n")
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = "entrypoint_not_found"
        result["error_excerpt"] = _tail_lines(log_path, 240)
        _write_json(results_path, result)
        return 1
    except Exception as e:
        with log_path.open("a", encoding="utf-8", errors="replace") as log_f:
            log_f.write(f"\nException: {e}\n")
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = "unknown"
        result["error_excerpt"] = _tail_lines(log_path, 240)
        _write_json(results_path, result)
        return 1

    # Determine outcome.
    if rc == 0:
        result["status"] = "success"
        result["exit_code"] = 0
        result["failure_category"] = "unknown"
        result["error_excerpt"] = ""
        _write_json(results_path, result)
        return 0

    # Non-zero exit: failure
    result["status"] = "failure"
    result["exit_code"] = 1
    if result["failure_category"] == "unknown":
        result["failure_category"] = "runtime"
    result["error_excerpt"] = _tail_lines(log_path, 240)
    _write_json(results_path, result)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Unified benchmark command runner.")
    p.add_argument("--stage", required=True, help="Stage name (e.g., pyright, prepare, cpu).")
    p.add_argument("--task", required=True, help="Task name (check, download, infer, validate, measure).")
    p.add_argument("--timeout-sec", type=int, required=True, help="Timeout in seconds.")
    p.add_argument("--out-dir", default="", help="Output directory (default: build_output/<stage>).")
    p.add_argument("--framework", default="auto", help="pytorch|tensorflow|jax|unknown|auto")
    p.add_argument("--decision-reason", default="", help="Why this command was chosen.")
    p.add_argument("--report-path", default="", help="Override report path (default: /opt/scimlopsbench/report.json).")
    p.add_argument("--python", default="", help="Override python interpreter for resolution.")
    p.add_argument(
        "--requires-python",
        action="store_true",
        help="Fail if python cannot be resolved via report/overrides.",
    )
    p.add_argument("--skip", action="store_true", help="Skip stage without running command.")
    p.add_argument(
        "--skip-reason",
        default="unknown",
        help="repo_not_supported|insufficient_hardware|not_applicable|unknown",
    )
    p.add_argument("--failure-category", default="", help="Override failure category.")
    p.add_argument(
        "--env",
        action="append",
        default=[],
        help="Extra env var KEY=VAL (repeatable).",
    )
    p.add_argument(
        "--assets-json",
        default="",
        help="Path to JSON containing assets object to embed into results.",
    )
    p.add_argument(
        "--meta-json",
        default="",
        help="Path to JSON containing extra meta to merge into results.",
    )
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="Command after --")

    args = p.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else (REPO_ROOT / "build_output" / args.stage)
    report_path = _resolve_report_path(args.report_path or None)

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if not args.skip and not cmd:
        _safe_mkdir(out_dir)
        log_path = out_dir / "log.txt"
        results_path = out_dir / "results.json"
        _write_text(log_path, "No command provided.\n")
        result = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": args.stage,
            "task": args.task,
            "command": "",
            "timeout_sec": int(args.timeout_sec),
            "framework": args.framework,
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "meta": {
                "python": "",
                "git_commit": _git_commit(REPO_ROOT),
                "env_vars": {},
                "decision_reason": args.decision_reason,
                "timestamp_utc": _utc_timestamp(),
            },
            "failure_category": "args_unknown",
            "error_excerpt": _tail_lines(log_path, 80),
        }
        _write_json(results_path, result)
        return 1

    extra_env, env_err = _parse_kv_list(args.env)
    if env_err:
        return 2

    assets_obj: Optional[dict] = None
    if args.assets_json:
        try:
            assets_obj = json.loads(_read_text(Path(args.assets_json)))
        except Exception:
            assets_obj = None

    meta_obj: Optional[dict] = None
    if args.meta_json:
        try:
            meta_obj = json.loads(_read_text(Path(args.meta_json)))
        except Exception:
            meta_obj = None

    return run_command(
        stage=args.stage,
        task=args.task,
        command_argv=cmd,
        timeout_sec=args.timeout_sec,
        out_dir=out_dir,
        requires_python=bool(args.requires_python),
        report_path=report_path,
        cli_python=args.python or None,
        framework=args.framework,
        decision_reason=args.decision_reason,
        skip=bool(args.skip),
        skip_reason=args.skip_reason,
        failure_category=args.failure_category or None,
        extra_assets=assets_obj,
        extra_meta=meta_obj,
        extra_env=extra_env,
    )


if __name__ == "__main__":
    raise SystemExit(main())

