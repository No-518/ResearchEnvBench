#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"

DEFAULT_TIMEOUTS_SEC: dict[str, int] = {
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            dq: deque[str] = deque(maxlen=max_lines)
            for line in f:
                dq.append(line.rstrip("\n"))
        return "\n".join(dq)
    except FileNotFoundError:
        return ""
    except Exception as e:  # noqa: BLE001
        return f"<failed to read log tail: {e}>"


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
    except Exception:  # noqa: BLE001
        return ""


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "json_not_object"
        return data, None
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:  # noqa: BLE001
        return None, f"invalid_json: {e}"


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def _is_executable_file(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except Exception:  # noqa: BLE001
        return False


def _mask_env_value(key: str, value: str) -> str:
    upper = key.upper()
    if any(tok in upper for tok in ("TOKEN", "PASSWORD", "SECRET", "KEY")):
        if not value:
            return ""
        return "<redacted>"
    return value


def _collect_env_meta(env_overrides: dict[str, str]) -> dict[str, str]:
    keys_of_interest = {
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "DIFFUSERS_CACHE",
        "HF_DATASETS_CACHE",
        "XDG_CACHE_HOME",
        "TORCH_HOME",
        "PIP_CACHE_DIR",
        "HF_HUB_OFFLINE",
        "HF_HUB_DISABLE_TELEMETRY",
        "TOKENIZERS_PARALLELISM",
        "HF_TOKEN",
        "HF_AUTH_TOKEN",
        "HUGGINGFACE_HUB_TOKEN",
    }
    merged: dict[str, str] = {}
    for k in sorted(keys_of_interest):
        if k in env_overrides:
            merged[k] = _mask_env_value(k, str(env_overrides[k]))
        elif k in os.environ:
            merged[k] = _mask_env_value(k, str(os.environ.get(k, "")))
    return merged


class _PythonResolutionError(Exception):
    def __init__(self, failure_category: str, message: str) -> None:
        super().__init__(message)
        self.failure_category = failure_category
        self.message = message


def resolve_python_interpreter(
    *,
    cli_python: str | None,
    requires_python: bool,
    report_path: Path,
) -> tuple[str | None, dict[str, Any]]:
    meta: dict[str, Any] = {"report_path": str(report_path), "python_resolution": {}}
    warnings: list[str] = []

    if cli_python:
        meta["python_resolution"] = {"source": "cli", "python": cli_python}
        return cli_python, meta

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        meta["python_resolution"] = {"source": "env", "python": env_python}
        return env_python, meta

    report, report_err = _load_json(report_path)
    if report is None:
        if requires_python:
            raise _PythonResolutionError(
                "missing_report",
                f"Report missing/invalid at {report_path}: {report_err}",
            )
        meta["python_resolution"] = {"source": "none", "python": None, "report_error": report_err}
        return None, meta

    reported_python = report.get("python_path")
    meta["python_resolution"] = {
        "source": "report",
        "python": reported_python,
        "report_error": None,
    }

    if isinstance(reported_python, str) and reported_python:
        if _is_executable_file(reported_python):
            return reported_python, meta
        warnings.append(f"python_path is not executable: {reported_python}; falling back to PATH python")
    else:
        warnings.append("python_path missing in report; falling back to PATH python")

    fallback = shutil.which("python3") or shutil.which("python") or sys.executable
    if not fallback:
        if requires_python:
            raise _PythonResolutionError("missing_report", "No python available in PATH for fallback resolution")
        meta["python_resolution"] = {"source": "none", "python": None}
        return None, meta

    meta["python_resolution"] = {"source": "path_fallback", "python": fallback}
    meta["python_resolution_warnings"] = warnings
    return fallback, meta


def _default_assets() -> dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _load_assets_from(path: Path) -> tuple[dict[str, Any], str | None]:
    data, err = _load_json(path)
    if data is None:
        return _default_assets(), err
    assets = data.get("assets")
    if isinstance(assets, dict):
        dataset = assets.get("dataset") if isinstance(assets.get("dataset"), dict) else {}
        model = assets.get("model") if isinstance(assets.get("model"), dict) else {}
        return {
            "dataset": {**_default_assets()["dataset"], **dataset},
            "model": {**_default_assets()["model"], **model},
        }, None
    if any(k in data for k in ("dataset", "model")):
        dataset = data.get("dataset") if isinstance(data.get("dataset"), dict) else {}
        model = data.get("model") if isinstance(data.get("model"), dict) else {}
        return {
            "dataset": {**_default_assets()["dataset"], **dataset},
            "model": {**_default_assets()["model"], **model},
        }, None
    return _default_assets(), "missing_assets"


def run_with_results(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark stage runner with logging and results.json.")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--framework", default="unknown")
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--out-dir", default=None, help="Default: build_output/<stage>")
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python", default=None, help="Explicit python executable (highest priority).")
    parser.add_argument(
        "--requires-python",
        action="store_true",
        help="If set, missing/invalid report.json without --python causes a stage failure.",
    )
    parser.add_argument("--assets-from", default=None, help="Path to another stage results.json containing assets.")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument(
        "--skip",
        action="store_true",
        help="Do not execute; write a skipped results.json and exit 0.",
    )
    parser.add_argument(
        "--skip-reason",
        default="unknown",
        help="repo_not_supported|insufficient_hardware|not_applicable|unknown",
    )
    parser.add_argument("--failure-category", default="")
    parser.add_argument("--command", default=None, help="Override command string recorded in results.json.")
    parser.add_argument("--env", action="append", default=[], help="KEY=VAL (repeatable) applied to subprocess env.")
    parser.add_argument(
        "--print-python",
        action="store_true",
        help="Print resolved python path and exit (does not write results.json).",
    )
    parser.add_argument("--python-script", default=None, help="Run a Python script via resolved python.")
    parser.add_argument("--python-module", default=None, help="Run a Python module via resolved python (-m).")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run, after '--'.")

    args = parser.parse_args(argv)

    repo_root = _repo_root()
    stage = args.stage
    out_dir = Path(args.out_dir) if args.out_dir else repo_root / "build_output" / stage
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = args.timeout_sec
    if timeout_sec is None:
        timeout_sec = DEFAULT_TIMEOUTS_SEC.get(stage, 600)

    env_overrides: dict[str, str] = {}
    for item in args.env:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        env_overrides[k] = v

    report_path = _resolve_report_path(args.report_path)

    needs_python = bool(args.requires_python or args.python_script or args.python_module)
    if args.print_python:
        try:
            python_exe, py_meta = resolve_python_interpreter(
                cli_python=args.python,
                requires_python=True,
                report_path=report_path,
            )
            if not python_exe:
                raise _PythonResolutionError("missing_report", "No python resolved")
            print(python_exe)
            return 0
        except _PythonResolutionError as e:
            print(e.message, file=sys.stderr)
            return 1

    result: dict[str, Any] = {
        "status": "failure",
        "skip_reason": args.skip_reason if args.skip else "unknown",
        "exit_code": 1,
        "stage": stage,
        "task": args.task,
        "command": "",
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": _default_assets(),
        "meta": {
            "python": "",
            "git_commit": _git_commit(repo_root),
            "env_vars": _collect_env_meta(env_overrides),
            "decision_reason": args.decision_reason,
            "timestamp_utc": _utc_now_iso(),
        },
        "failure_category": args.failure_category or "unknown",
        "error_excerpt": "",
    }

    _safe_mkdir(out_dir)

    assets_err: str | None = None
    if args.assets_from:
        assets, assets_err = _load_assets_from(Path(args.assets_from))
        result["assets"] = assets
        if assets_err:
            result["meta"]["assets_warning"] = f"assets_from_error={assets_err}"

    python_exe: str | None = None
    py_resolution_meta: dict[str, Any] = {}
    try:
        if needs_python:
            python_exe, py_resolution_meta = resolve_python_interpreter(
                cli_python=args.python,
                requires_python=True,
                report_path=report_path,
            )
            if not python_exe:
                raise _PythonResolutionError("missing_report", "No python resolved for python-required stage")
        else:
            python_exe, py_resolution_meta = resolve_python_interpreter(
                cli_python=args.python,
                requires_python=False,
                report_path=report_path,
            )
    except _PythonResolutionError as e:
        with log_path.open("w", encoding="utf-8", errors="replace") as lf:
            lf.write(f"[runner] {stage} failed resolving python: {e.message}\n")
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = e.failure_category
        result["command"] = args.command or ""
        result["error_excerpt"] = _tail_lines(log_path)
        result["meta"].update(py_resolution_meta)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    result["meta"].update(py_resolution_meta)
    if python_exe:
        result["meta"]["python"] = python_exe
        try:
            v = subprocess.check_output(
                [python_exe, "-c", "import platform; print(platform.python_version())"],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
            ).strip()
            result["meta"]["python_version"] = v
        except Exception as e:  # noqa: BLE001
            result["meta"]["python_version_warning"] = str(e)

    # Build command.
    cmd: list[str] = []
    if args.python_script:
        cmd = [python_exe or "python", args.python_script, *args.cmd[1:]] if args.cmd[:1] == ["--"] else [
            python_exe or "python",
            args.python_script,
            *args.cmd,
        ]
    elif args.python_module:
        cmd = [python_exe or "python", "-m", args.python_module, *args.cmd[1:]] if args.cmd[:1] == ["--"] else [
            python_exe or "python",
            "-m",
            args.python_module,
            *args.cmd,
        ]
    else:
        cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else list(args.cmd)

    if args.skip:
        with log_path.open("w", encoding="utf-8", errors="replace") as lf:
            lf.write(f"[runner] stage={stage} skipped\n")
            lf.write(f"[runner] skip_reason={args.skip_reason}\n")
            if args.decision_reason:
                lf.write(f"[runner] decision_reason={args.decision_reason}\n")
        result["status"] = "skipped"
        result["skip_reason"] = args.skip_reason
        result["exit_code"] = 0
        result["failure_category"] = args.failure_category or ""
        if args.command:
            result["command"] = args.command
        else:
            result["command"] = shlex.join(cmd) if cmd else ""
        result["error_excerpt"] = _tail_lines(log_path)
        if (
            result["status"] == "failure"
            and stage in ("cpu", "single_gpu", "multi_gpu")
            and result.get("failure_category") in ("unknown", "runtime")
        ):
            inferred = _infer_failure_category_from_log(result["error_excerpt"])
            if inferred:
                result["failure_category"] = inferred
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    if not cmd and not args.command:
        with log_path.open("w", encoding="utf-8", errors="replace") as lf:
            lf.write("[runner] no command provided\n")
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = args.failure_category or "args_unknown"
        result["command"] = ""
        result["error_excerpt"] = _tail_lines(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    if args.command:
        # Use provided command string for recording, but still execute cmd list if present.
        result["command"] = args.command
    else:
        result["command"] = shlex.join(cmd)

    env = os.environ.copy()
    env.update(env_overrides)

    started = time.time()
    timed_out = False
    command_returncode: int | None = None

    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as lf:
            lf.write(f"[runner] stage={stage} task={args.task}\n")
            lf.write(f"[runner] timestamp_utc={_utc_now_iso()}\n")
            lf.write(f"[runner] cwd={repo_root}\n")
            lf.write(f"[runner] timeout_sec={timeout_sec}\n")
            lf.write(f"[runner] command={result['command']}\n\n")
            lf.flush()

            proc = subprocess.Popen(
                cmd,
                cwd=str(repo_root),
                env=env,
                stdout=lf,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                command_returncode = proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                try:
                    command_returncode = proc.wait(timeout=15)
                except Exception:  # noqa: BLE001
                    command_returncode = None
                lf.write("\n[runner] TIMEOUT\n")
    except FileNotFoundError as e:
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = args.failure_category or "entrypoint_not_found"
        with log_path.open("a", encoding="utf-8", errors="replace") as lf:
            lf.write(f"\n[runner] FileNotFoundError: {e}\n")
    except Exception:  # noqa: BLE001
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = args.failure_category or "runtime"
        with log_path.open("a", encoding="utf-8", errors="replace") as lf:
            lf.write("\n[runner] exception:\n")
            lf.write(traceback.format_exc())
    finally:
        elapsed = time.time() - started
        result["meta"]["elapsed_sec"] = round(elapsed, 3)
        if command_returncode is not None:
            result["meta"]["command_returncode"] = command_returncode
        if timed_out:
            result["failure_category"] = args.failure_category or "timeout"

        if not timed_out and command_returncode == 0:
            result["status"] = "success"
            result["exit_code"] = 0
            result["failure_category"] = ""
        elif result["status"] != "failure":
            result["status"] = "failure"
            result["exit_code"] = 1
            if not result.get("failure_category"):
                result["failure_category"] = args.failure_category or "runtime"

        result["error_excerpt"] = _tail_lines(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return 0 if result["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run_with_results())
