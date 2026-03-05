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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = _read_text(path).splitlines()
    except Exception:
        return ""
    tail = lines[-max_lines:]
    return "\n".join(tail)


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.STDOUT, text=True)
        return out.strip()
    except Exception:
        return ""


def _default_timeout(stage: str) -> int:
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


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def _load_report(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not report_path.exists():
        return None, "missing_report"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def _which_python() -> Optional[str]:
    return shutil.which("python") or shutil.which("python3")


def resolve_python_path(
    *,
    cli_python: Optional[str],
    report_path: Path,
    require_report_if_no_override: bool,
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """
    Returns: (python_path or None, meta, failure_category or None)
    """
    meta: Dict[str, Any] = {
        "report_path": str(report_path),
        "python_resolution": {
            "used_cli": False,
            "used_env": False,
            "used_report": False,
            "used_fallback": False,
            "warnings": [],
        },
    }

    if cli_python:
        meta["python_resolution"]["used_cli"] = True
        return cli_python, meta, None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        meta["python_resolution"]["used_env"] = True
        return env_python, meta, None

    report, err = _load_report(report_path)
    if err:
        if require_report_if_no_override:
            meta["python_resolution"]["warnings"].append(f"report_load_failed:{err}")
            return None, meta, "missing_report"
        fallback = _which_python()
        if fallback:
            meta["python_resolution"]["used_fallback"] = True
            meta["python_resolution"]["warnings"].append(f"using_fallback_python:{fallback}")
            return fallback, meta, None
        return None, meta, "missing_report"

    python_path = report.get("python_path")
    if isinstance(python_path, str) and python_path:
        meta["python_resolution"]["used_report"] = True
        return python_path, meta, None

    fallback = _which_python()
    if fallback:
        meta["python_resolution"]["used_fallback"] = True
        meta["python_resolution"]["warnings"].append("report_missing_python_path_using_fallback")
        meta["python_resolution"]["warnings"].append(f"using_fallback_python:{fallback}")
        return fallback, meta, None

    meta["python_resolution"]["warnings"].append("report_missing_python_path_and_no_fallback")
    return None, meta, "path_hallucination"


def _select_env_vars(env: Dict[str, str]) -> Dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "SENTENCE_TRANSFORMERS_HOME",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "PYTHONDONTWRITEBYTECODE",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
    ]
    out: Dict[str, str] = {}
    for k in keys:
        if k in env:
            out[k] = env[k]
    return out


def _load_assets_from_prepare(repo_root: Path) -> Dict[str, Any]:
    prepare_results = repo_root / "build_output" / "prepare" / "results.json"
    if not prepare_results.exists():
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    try:
        data = json.loads(prepare_results.read_text(encoding="utf-8"))
        assets = data.get("assets")
        if isinstance(assets, dict):
            return assets
    except Exception:
        pass
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _base_results(
    *,
    stage: str,
    task: str,
    command_str: str,
    timeout_sec: int,
    framework: str,
    repo_root: Path,
    env_vars: Dict[str, str],
    decision_reason: str,
    python_meta: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": task,
        "command": command_str,
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": _load_assets_from_prepare(repo_root),
        "meta": {
            "timestamp_utc": _utc_timestamp(),
            "python": "",
            "git_commit": _git_commit(repo_root),
            "env_vars": env_vars,
            "decision_reason": decision_reason,
            **python_meta,
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }


def _run_command(
    *,
    argv: List[str],
    cwd: Path,
    env: Dict[str, str],
    timeout_sec: int,
    log_path: Path,
) -> Tuple[int, bool, Optional[str]]:
    try:
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"[runner] utc={_utc_timestamp()}\n")
            log.write(f"[runner] cwd={cwd}\n")
            log.write(f"[runner] argv={shlex.join(argv)}\n")
            log.flush()
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_sec,
            )
        return int(completed.returncode), False, None
    except FileNotFoundError as e:
        _write_text(log_path, f"[runner] FileNotFoundError: {e}\n")
        return 127, False, "entrypoint_not_found"
    except subprocess.TimeoutExpired:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n[runner] TIMEOUT after {timeout_sec}s\n")
        return 124, True, "timeout"
    except Exception as e:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n[runner] Exception: {e}\n")
        return 1, False, "runtime"


def cmd_resolve_python(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    report_path = _resolve_report_path(args.report_path)
    resolved, meta, failure = resolve_python_path(
        cli_python=args.python,
        report_path=report_path,
        require_report_if_no_override=not args.allow_fallback,
    )
    if resolved:
        sys.stdout.write(resolved + "\n")
        return 0
    sys.stderr.write(json.dumps({"error": failure or "unknown", "meta": meta}, indent=2) + "\n")
    return 1


def cmd_skip(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    out_dir = Path(args.out_dir) if args.out_dir else (repo_root / "build_output" / args.stage)
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    env_vars = _select_env_vars(dict(os.environ))
    results = _base_results(
        stage=args.stage,
        task=args.task,
        command_str=args.command or "",
        timeout_sec=int(args.timeout_sec or _default_timeout(args.stage)),
        framework=args.framework,
        repo_root=repo_root,
        env_vars=env_vars,
        decision_reason=args.decision_reason or "",
        python_meta={"python_resolution": {"skipped": True}},
    )
    results["status"] = "skipped"
    results["skip_reason"] = args.skip_reason
    results["exit_code"] = 0
    results["failure_category"] = "unknown"
    results["error_excerpt"] = ""

    _write_text(log_path, f"[runner] skipped stage={args.stage} reason={args.skip_reason}\n")
    _write_json(results_path, results)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    out_dir = Path(args.out_dir) if args.out_dir else (repo_root / "build_output" / args.stage)
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = int(args.timeout_sec or _default_timeout(args.stage))

    base_env = dict(os.environ)
    base_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    base_env.setdefault("PYTHONUNBUFFERED", "1")
    for kv in args.env or []:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        base_env[k] = v

    resolved_python = None
    python_meta: Dict[str, Any] = {}
    if not args.no_python_needed:
        report_path = _resolve_report_path(args.report_path)
        resolved_python, python_meta, failure = resolve_python_path(
            cli_python=args.python,
            report_path=report_path,
            require_report_if_no_override=True,
        )
        if not resolved_python:
            env_vars = _select_env_vars(base_env)
            results = _base_results(
                stage=args.stage,
                task=args.task,
                command_str=args.command or "",
                timeout_sec=timeout_sec,
                framework=args.framework,
                repo_root=repo_root,
                env_vars=env_vars,
                decision_reason=args.decision_reason or "",
                python_meta=python_meta,
            )
            results["status"] = "failure"
            results["exit_code"] = 1
            results["failure_category"] = failure or "missing_report"
            _write_text(log_path, f"[runner] python resolution failed: {results['failure_category']}\n")
            results["error_excerpt"] = _tail_lines(log_path)
            _write_json(results_path, results)
            return 1

    argv = list(args.argv)
    if argv and argv[0] == "@python":
        if not resolved_python:
            argv[0] = _which_python() or "python"
        else:
            argv[0] = resolved_python

    command_str = args.command or shlex.join(argv)

    env_vars = _select_env_vars(base_env)
    results = _base_results(
        stage=args.stage,
        task=args.task,
        command_str=command_str,
        timeout_sec=timeout_sec,
        framework=args.framework,
        repo_root=repo_root,
        env_vars=env_vars,
        decision_reason=args.decision_reason or "",
        python_meta=python_meta,
    )
    if resolved_python:
        results["meta"]["python"] = resolved_python

    rc, timed_out, rc_category = _run_command(argv=argv, cwd=repo_root, env=base_env, timeout_sec=timeout_sec, log_path=log_path)
    results["meta"]["process_returncode"] = rc
    results["meta"]["timed_out"] = bool(timed_out)

    if rc == 0:
        results["status"] = "success"
        results["exit_code"] = 0
        results["skip_reason"] = "not_applicable"
        results["failure_category"] = "unknown"
    else:
        results["status"] = "failure"
        results["exit_code"] = 1
        results["skip_reason"] = "unknown"
        if timed_out:
            results["failure_category"] = "timeout"
        elif rc_category:
            results["failure_category"] = rc_category
        else:
            results["failure_category"] = "runtime"

    results["error_excerpt"] = _tail_lines(log_path)
    _write_json(results_path, results)
    return 0 if results["status"] in ("success", "skipped") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Unified benchmark command runner (writes log.txt and results.json).")
    sub = p.add_subparsers(dest="subcommand", required=True)

    sp = sub.add_parser("resolve-python", help="Print resolved python executable path.")
    sp.add_argument("--python", default=None, help="Explicit python path (highest priority).")
    sp.add_argument("--report-path", default=None, help="Override report.json path.")
    sp.add_argument("--allow-fallback", action="store_true", help="Allow fallback to python from PATH.")
    sp.set_defaults(func=cmd_resolve_python)

    sp = sub.add_parser("skip", help="Write a skipped stage results.json/log.txt.")
    sp.add_argument("--stage", required=True)
    sp.add_argument("--task", required=True)
    sp.add_argument("--framework", default="unknown")
    sp.add_argument("--out-dir", default=None)
    sp.add_argument("--timeout-sec", default=None)
    sp.add_argument("--skip-reason", required=True, choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"])
    sp.add_argument("--decision-reason", default="")
    sp.add_argument("--command", default="")
    sp.set_defaults(func=cmd_skip)

    sp = sub.add_parser("run", help="Run a command and write stage results.")
    sp.add_argument("--stage", required=True)
    sp.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    sp.add_argument("--framework", default="unknown")
    sp.add_argument("--out-dir", default=None)
    sp.add_argument("--timeout-sec", default=None)
    sp.add_argument("--python", default=None, help="Explicit python path used to replace @python.")
    sp.add_argument("--report-path", default=None, help="Override report.json path for python resolution.")
    sp.add_argument("--decision-reason", default="")
    sp.add_argument("--env", action="append", default=[], help="Extra env var KEY=VAL (repeatable).")
    sp.add_argument("--no-python-needed", action="store_true", help="Do not require report/python resolution.")
    sp.add_argument("--command", default="", help="Command string to record (optional).")
    sp.add_argument("argv", nargs=argparse.REMAINDER, help="Command to run after --")
    sp.set_defaults(func=cmd_run)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    # Strip leading '--' if present (common when using argparse.REMAINDER)
    if getattr(args, "argv", None) and args.argv and args.argv[0] == "--":
        args.argv = args.argv[1:]
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

