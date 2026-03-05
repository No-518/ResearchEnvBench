#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _safe_read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        tail = lines[-max_lines:]
        return "\n".join(tail)
    except Exception:
        return ""


def _minimal_env_snapshot(env: Dict[str, str]) -> Dict[str, str]:
    keep_keys = [
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "TRANSFORMERS_CACHE",
        "HF_DATASETS_CACHE",
        "SENTENCE_TRANSFORMERS_HOME",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "PIP_CACHE_DIR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "SCIMLOPSBENCH_PYTHON",
        "SCIMLOPSBENCH_REPORT",
    ]
    out: Dict[str, str] = {}
    for k in keep_keys:
        v = env.get(k)
        if v is not None:
            out[k] = v
    return out


def _default_cache_env(repo_root: Path) -> Dict[str, str]:
    bench_assets = repo_root / "benchmark_assets"
    bench_cache = bench_assets / "cache"
    _ensure_dir(bench_cache)
    d = {
        "XDG_CACHE_HOME": str(bench_cache / "xdg"),
        "PIP_CACHE_DIR": str(bench_cache / "pip"),
        "HF_HOME": str(bench_cache / "huggingface"),
        "TRANSFORMERS_CACHE": str(bench_cache / "huggingface" / "transformers"),
        "HF_DATASETS_CACHE": str(bench_cache / "huggingface" / "datasets"),
        "TORCH_HOME": str(bench_cache / "torch"),
        "SENTENCE_TRANSFORMERS_HOME": str(bench_cache / "sentence_transformers"),
        "TMPDIR": str(bench_cache / "tmp"),
        "TEMP": str(bench_cache / "tmp"),
        "TMP": str(bench_cache / "tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    for p in [
        Path(d["XDG_CACHE_HOME"]),
        Path(d["PIP_CACHE_DIR"]),
        Path(d["HF_HOME"]),
        Path(d["TRANSFORMERS_CACHE"]),
        Path(d["HF_DATASETS_CACHE"]),
        Path(d["TORCH_HOME"]),
        Path(d["SENTENCE_TRANSFORMERS_HOME"]),
        Path(d["TMPDIR"]),
    ]:
        _ensure_dir(p)
    return d


def _load_report_json(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not report_path.exists():
        return None, f"Report not found: {report_path}"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "Report JSON is not an object"
        return data, None
    except Exception as e:
        return None, f"Failed to parse report JSON: {e}"


@dataclass
class PythonResolution:
    python: str
    source: str
    warning: str = ""


def resolve_python(
    cli_python: Optional[str],
    report_path: Path,
    require_report_if_needed: bool,
) -> Tuple[Optional[PythonResolution], Optional[str]]:
    if cli_python:
        return PythonResolution(python=cli_python, source="cli"), None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return PythonResolution(python=env_python, source="env"), None

    report, err = _load_report_json(report_path)
    if report is not None:
        python_path = report.get("python_path")
        if isinstance(python_path, str) and python_path.strip():
            return PythonResolution(python=python_path, source="report"), None
        if require_report_if_needed:
            return None, "Report is missing non-empty 'python_path'"

    if require_report_if_needed:
        return None, err or "Report missing/invalid and no --python provided"

    return PythonResolution(python="python", source="path", warning="Using fallback 'python' from PATH"), None


def _cmd_to_str(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _merge_dict(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in src.items():
        if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
            dst[k] = _merge_dict(dst[k], v)
        else:
            dst[k] = v
    return dst


def runner_main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Unified stage runner for scimlopsbench.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_resolve = sub.add_parser("resolve-python", help="Print resolved python executable.")
    p_resolve.add_argument("--python", default="", help="Explicit python path (highest priority).")
    p_resolve.add_argument("--report-path", default="", help="Override report path.")
    p_resolve.add_argument("--require-report", action="store_true", help="Fail if report missing/invalid.")

    p_run = sub.add_parser("run", help="Run a command with logging + results.json.")
    p_run.add_argument("--stage", required=True, help="Stage name (e.g., cpu, prepare).")
    p_run.add_argument("--task", required=True, help="Task type (train|infer|check|download|validate|measure).")
    p_run.add_argument("--framework", default="unknown", help="Framework (pytorch|tensorflow|jax|unknown).")
    p_run.add_argument("--timeout-sec", type=int, default=0, help="Timeout seconds (0 => stage default).")
    p_run.add_argument("--out-dir", default="", help="Output dir (default: build_output/<stage>).")
    p_run.add_argument("--python", default="", help="Explicit python path for {python} substitution.")
    p_run.add_argument("--report-path", default="", help="Override report path.")
    p_run.add_argument("--requires-python", action="store_true", help="Fail if report missing/invalid and no --python.")
    p_run.add_argument("--decision-reason", default="", help="Why this command/params were chosen.")
    p_run.add_argument("--assets-json", default="", help="JSON file to merge into results['assets'].")
    p_run.add_argument("--meta-json", default="", help="JSON file to merge into results['meta'].")
    p_run.add_argument("--extra-results-json", default="", help="JSON file to merge into top-level results.")
    p_run.add_argument("--skip", action="store_true", help="Mark stage skipped and do not execute command.")
    p_run.add_argument("--skip-reason", default="unknown", help="Skip reason.")
    p_run.add_argument("--failure-category", default="", help="Override failure_category on failure.")
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="Command argv; use {python} placeholder if needed.")

    args = parser.parse_args(argv)
    repo_root = _repo_root()

    stage_defaults = {
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

    if args.cmd == "resolve-python":
        report_path = Path(args.report_path) if args.report_path else Path(os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH))
        res, err = resolve_python(
            cli_python=args.python or None,
            report_path=report_path,
            require_report_if_needed=bool(args.require_report),
        )
        if err:
            print(err, file=sys.stderr)
            return 1
        assert res is not None
        print(res.python)
        return 0

    # run
    stage: str = args.stage
    task: str = args.task
    out_dir = Path(args.out_dir) if args.out_dir else (repo_root / "build_output" / stage)
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = int(args.timeout_sec) if args.timeout_sec else int(stage_defaults.get(stage, 600))
    report_path = Path(args.report_path) if args.report_path else Path(os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH))

    base_env = os.environ.copy()
    base_env.update(_default_cache_env(repo_root))

    command_argv = list(args.command)
    if command_argv and command_argv[0] == "--":
        command_argv = command_argv[1:]

    python_res: Optional[PythonResolution] = None
    python_err: Optional[str] = None
    if args.requires_python or any("{python}" in a for a in command_argv):
        python_res, python_err = resolve_python(
            cli_python=args.python or None,
            report_path=report_path,
            require_report_if_needed=True,
        )
        if python_res is None:
            with log_path.open("w", encoding="utf-8") as logf:
                logf.write(f"[runner] python resolution failed: {python_err}\n")
            results = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": stage,
                "task": task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": args.framework,
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "meta": {
                    "python": "",
                    "git_commit": _git_commit(repo_root),
                    "env_vars": _minimal_env_snapshot(base_env),
                    "decision_reason": args.decision_reason,
                    "timestamp_utc": _utc_timestamp(),
                },
                "failure_category": "missing_report",
                "error_excerpt": python_err or "",
            }
            _write_json(results_path, results)
            return 1

        command_argv = [a.replace("{python}", python_res.python) for a in command_argv]

    cmd_str = _cmd_to_str(command_argv) if command_argv else ""

    # Initialize base results.
    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": task,
        "command": cmd_str,
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": (python_res.python if python_res else ""),
            "python_source": (python_res.source if python_res else ""),
            "python_warning": (python_res.warning if python_res else ""),
            "git_commit": _git_commit(repo_root),
            "env_vars": _minimal_env_snapshot(base_env),
            "decision_reason": args.decision_reason,
            "timestamp_utc": _utc_timestamp(),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    # Merge assets/meta/extra.
    for key, path_str in [("assets", args.assets_json), ("meta", args.meta_json)]:
        if path_str:
            p = Path(path_str)
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(results.get(key), dict):
                    # Accept either a direct dict (e.g., {"dataset": ...}) or a full stage results.json
                    # containing an "assets"/"meta" key.
                    if key in payload and isinstance(payload.get(key), dict):
                        payload = payload[key]  # type: ignore[assignment]
                    results[key] = _merge_dict(results[key], payload)  # type: ignore[arg-type]
            except Exception as e:
                results["meta"]["warnings"] = results["meta"].get("warnings", []) + [f"Failed to merge {key} from {p}: {e}"]

    if args.extra_results_json:
        p = Path(args.extra_results_json)
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                results = _merge_dict(results, payload)
        except Exception as e:
            results["meta"]["warnings"] = results["meta"].get("warnings", []) + [f"Failed to merge extra results from {p}: {e}"]

    if args.skip:
        results["status"] = "skipped"
        results["skip_reason"] = args.skip_reason or "unknown"
        results["exit_code"] = 0
        results["failure_category"] = "unknown"
        results["error_excerpt"] = ""
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write(f"[runner] skipped stage={stage} reason={results['skip_reason']}\n")
        _write_json(results_path, results)
        return 0

    if not command_argv:
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write("[runner] no command provided\n")
        results["failure_category"] = "entrypoint_not_found"
        results["error_excerpt"] = "No command provided"
        _write_json(results_path, results)
        return 1

    # Execute.
    start = time.time()
    timed_out = False
    rc: Optional[int] = None
    try:
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write(f"[runner] repo_root={repo_root}\n")
            logf.write(f"[runner] stage={stage} task={task} timeout_sec={timeout_sec}\n")
            if python_res:
                logf.write(f"[runner] resolved_python={python_res.python} source={python_res.source}\n")
                if python_res.warning:
                    logf.write(f"[runner] python_warning={python_res.warning}\n")
            logf.write(f"[runner] command={cmd_str}\n")
            logf.flush()

            proc = subprocess.Popen(
                command_argv,
                cwd=str(repo_root),
                env=base_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            try:
                out, _ = proc.communicate(timeout=max(1, timeout_sec))
                if out:
                    logf.write(out)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                out, _ = proc.communicate(timeout=10)
                if out:
                    logf.write(out)
                rc = proc.returncode if proc.returncode is not None else 124
                logf.write(f"\n[runner] TIMEOUT after {timeout_sec}s\n")
    except FileNotFoundError as e:
        with log_path.open("a", encoding="utf-8") as logf:
            logf.write(f"\n[runner] FileNotFoundError: {e}\n")
        rc = 127
    except Exception as e:
        with log_path.open("a", encoding="utf-8") as logf:
            logf.write(f"\n[runner] Exception: {e}\n")
        rc = 1

    elapsed = time.time() - start
    results["meta"]["elapsed_sec"] = round(elapsed, 3)
    results["exit_code"] = 0 if (rc == 0) else 1
    results["status"] = "success" if (rc == 0) else "failure"

    if timed_out:
        results["failure_category"] = "timeout"
    elif rc == 127:
        results["failure_category"] = "entrypoint_not_found"
    elif rc == 0:
        results["failure_category"] = "unknown"
    else:
        results["failure_category"] = args.failure_category or "runtime"

    results["error_excerpt"] = _tail_lines(log_path, max_lines=220)
    _write_json(results_path, results)
    return 0 if results["status"] in ("success", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(runner_main(sys.argv[1:]))
