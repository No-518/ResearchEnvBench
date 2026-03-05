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


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_read_text(path: Path, max_bytes: int = 2_000_000) -> str:
    try:
        data = path.read_bytes()
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _tail_lines(path: Path, max_lines: int = 200) -> str:
    text = _safe_read_text(path)
    if not text:
        return ""
    lines = text.splitlines()
    tail = lines[-max_lines:]
    return "\n".join(tail)


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


def _empty_assets() -> Dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, f"missing: {path}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, f"invalid_json: {path}: {e}"


@dataclass
class PythonResolution:
    python: str
    source: str  # cli | env | report | fallback | none
    warning: str = ""


def _resolve_python(
    cli_python: str,
    report_path: Path,
) -> PythonResolution:
    if cli_python:
        return PythonResolution(python=cli_python, source="cli")

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON", "").strip()
    if env_python:
        return PythonResolution(python=env_python, source="env")

    report_obj, err = _load_json(report_path)
    if report_obj is None:
        raise RuntimeError(f"missing_report: {err}")

    python_path = report_obj.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        raise RuntimeError("missing_report: report.json missing python_path")

    python_path = python_path.strip()
    if os.path.exists(python_path) and os.access(python_path, os.X_OK):
        return PythonResolution(python=python_path, source="report")

    fallback = shutil.which("python3") or shutil.which("python") or ""
    if fallback:
        return PythonResolution(
            python=fallback,
            source="fallback",
            warning=f"report python_path not executable, fell back to PATH python: {fallback}",
        )
    return PythonResolution(
        python=python_path,
        source="report",
        warning="report python_path is not executable and no python found on PATH",
    )


def _python_version(python_exe: str) -> str:
    try:
        out = subprocess.check_output(
            [python_exe, "-c", "import platform; print(platform.python_version())"],
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


def _stringify_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    _ensure_parent(path)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="runner.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """\
            Unified benchmark command runner.

            - Writes <out-dir>/log.txt and <out-dir>/results.json (even on failure).
            - Replaces any '{python}' tokens in the command with the resolved python interpreter.
            """
        ),
    )

    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--timeout-sec", type=int, default=0)
    parser.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--failure-category", default="")
    parser.add_argument("--skip", action="store_true", help="Skip execution and emit a skipped results.json")
    parser.add_argument(
        "--skip-reason",
        default="unknown",
        choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"],
    )
    parser.add_argument("--skip-message", default="")

    parser.add_argument("--report-path", default=os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH))
    parser.add_argument("--python", default="", help="Explicit python executable (highest priority)")
    parser.add_argument(
        "--print-python",
        action="store_true",
        help="Print resolved python and exit (does not write stage outputs).",
    )

    parser.add_argument("--assets-from", default="", help="Path to a JSON file containing an 'assets' object")
    parser.add_argument("--env", action="append", default=[], help="Set env var KEY=VALUE for the command")
    parser.add_argument("--unset-env", action="append", default=[], help="Unset env var KEY for the command")

    parser.add_argument("--", dest="cmd_sep", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute (use '{python}' placeholder)")

    args = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = Path(args.out_dir) if args.out_dir else repo_root / "build_output" / args.stage
    out_dir.mkdir(parents=True, exist_ok=True)

    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = Path(args.report_path)

    default_timeouts = {
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
    timeout_sec = int(args.timeout_sec) if args.timeout_sec else int(default_timeouts.get(args.stage, 600))

    if args.print_python:
        try:
            res = _resolve_python(args.python, report_path)
        except Exception:
            return 1
        print(res.python)
        return 0

    # Always create log.txt early.
    log_path.touch(exist_ok=True)

    base_env = os.environ.copy()
    set_env: Dict[str, str] = {}
    for item in args.env:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        set_env[k] = v
    for k in args.unset_env:
        base_env.pop(k, None)
    base_env.update(set_env)

    assets: Dict[str, Any] = _empty_assets()
    assets_warning = ""
    if args.assets_from:
        assets_obj, aerr = _load_json(Path(args.assets_from))
        if assets_obj and isinstance(assets_obj.get("assets"), dict):
            assets = assets_obj["assets"]
        else:
            assets_warning = aerr or "assets_from missing 'assets' object"

    meta_env_keys = sorted(
        {
            "SCIMLOPSBENCH_PYTHON",
            "SCIMLOPSBENCH_REPORT",
            "CUDA_VISIBLE_DEVICES",
            "HF_HOME",
            "HF_HUB_CACHE",
            "TRANSFORMERS_CACHE",
            "TORCH_HOME",
            "XDG_CACHE_HOME",
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            *set_env.keys(),
        }
    )
    meta_env_vars = {k: base_env.get(k, "") for k in meta_env_keys}

    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": args.stage,
        "task": args.task,
        "command": "",
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": assets,
        "meta": {
            "python": "",
            "git_commit": _git_commit(repo_root),
            "env_vars": meta_env_vars,
            "decision_reason": args.decision_reason,
            "timestamp_utc": _utc_timestamp(),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    if assets_warning:
        results["meta"]["assets_warning"] = assets_warning

    if args.skip:
        results.update(
            {
                "status": "skipped",
                "skip_reason": args.skip_reason,
                "exit_code": 0,
                "command": args.skip_message or "skipped",
                "failure_category": "not_applicable",
                "error_excerpt": "",
            }
        )
        _write_json(results_path, results)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[runner] skipped: {args.skip_reason} {args.skip_message}\n")
        return 0

    if not args.cmd:
        results["failure_category"] = "args_unknown"
        results["error_excerpt"] = "No command provided."
        _write_json(results_path, results)
        with log_path.open("a", encoding="utf-8") as f:
            f.write("[runner] failure: no command provided\n")
        return 1

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    needs_python = any(part == "{python}" for part in cmd)
    py_res: Optional[PythonResolution] = None
    if needs_python:
        try:
            py_res = _resolve_python(args.python, report_path)
            results["meta"]["python"] = py_res.python
            results["meta"]["python_resolution"] = {"source": py_res.source, "warning": py_res.warning}
            results["meta"]["python_version"] = _python_version(py_res.python)
            cmd = [py_res.python if part == "{python}" else part for part in cmd]
        except Exception as e:
            msg = str(e)
            results["failure_category"] = "missing_report" if msg.startswith("missing_report") else "unknown"
            results["error_excerpt"] = msg
            results["command"] = _stringify_cmd(cmd)
            _write_json(results_path, results)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"[runner] python resolution failed: {msg}\n")
            return 1

    results["command"] = _stringify_cmd(cmd)

    start = time.time()
    exit_code = 1
    failure_category = ""

    try:
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write(f"[runner] cwd={repo_root}\n")
            if py_res and py_res.warning:
                log_f.write(f"[runner] python warning: {py_res.warning}\n")
            log_f.write(f"[runner] command: {results['command']}\n")
            log_f.flush()

            proc = subprocess.run(
                cmd,
                cwd=str(repo_root),
                env=base_env,
                stdout=log_f,
                stderr=log_f,
                text=True,
                timeout=timeout_sec,
            )
            exit_code = int(proc.returncode)
    except subprocess.TimeoutExpired:
        exit_code = 1
        failure_category = "timeout"
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write(f"[runner] timeout after {timeout_sec}s\n")
    except FileNotFoundError as e:
        exit_code = 1
        failure_category = "entrypoint_not_found"
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write(f"[runner] command not found: {e}\n")
    except Exception as e:
        exit_code = 1
        failure_category = "unknown"
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write(f"[runner] exception: {e}\n")

    elapsed = time.time() - start
    results["meta"]["elapsed_sec"] = round(elapsed, 3)

    if exit_code == 0:
        results["status"] = "success"
        results["exit_code"] = 0
        results["skip_reason"] = "not_applicable"
        results["failure_category"] = "unknown"
        results["error_excerpt"] = ""
        _write_json(results_path, results)
        return 0

    results["status"] = "failure"
    results["exit_code"] = 1
    results["skip_reason"] = "not_applicable"
    results["failure_category"] = args.failure_category or failure_category or "runtime"
    results["error_excerpt"] = _tail_lines(log_path, max_lines=220)
    if stage in ("cpu", "single_gpu", "multi_gpu") and results["failure_category"] in ("unknown", "runtime"):
        inferred = _infer_failure_category_from_log(results["error_excerpt"])
        if inferred:
            results["failure_category"] = inferred
    _write_json(results_path, results)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
