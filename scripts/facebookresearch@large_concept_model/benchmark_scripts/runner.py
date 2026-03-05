#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


DEFAULT_TIMEOUTS_SEC: Dict[str, int] = {
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


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _safe_read_text(path: Path, max_bytes: int = 2_000_000) -> str:
    try:
        with path.open("rb") as f:
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _tail_lines(text: str, max_lines: int = 200) -> str:
    lines = text.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
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


def _collect_env_vars() -> Dict[str, str]:
    prefixes = ("SCIMLOPSBENCH", "CUDA", "NCCL", "TORCH", "HF_", "TRANSFORMERS", "OMP_", "MKL_")
    out: Dict[str, str] = {}
    for k, v in os.environ.items():
        if k.startswith(prefixes):
            out[k] = v
    return out


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
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


def _read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"missing: {path}"
    except Exception as e:
        return None, f"read_error: {path}: {e}"
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, f"invalid_json_root_not_object: {path}"
        return data, None
    except Exception as e:
        return None, f"invalid_json: {path}: {e}"


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path("/opt/scimlopsbench/report.json")


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def _resolve_python(
    cli_python: Optional[str],
    report_path: Path,
) -> Tuple[Optional[str], List[str], Optional[str]]:
    """
    Returns (python_executable or None, warnings, resolution_source).
    On missing/invalid report with no CLI/env python: returns (None, warnings, None).
    """
    warnings: List[str] = []

    if cli_python:
        return cli_python, warnings, "cli"

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return env_python, warnings, "env:SCIMLOPSBENCH_PYTHON"

    report, err = _read_json(report_path)
    if report is None:
        warnings.append(f"missing_or_invalid_report: {err}")
        return None, warnings, None

    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        warnings.append("report_missing_python_path; falling back to PATH python")
        py = shutil.which("python") or shutil.which("python3")
        if py:
            return py, warnings, "path_fallback"
        return None, warnings, None

    py_path = Path(python_path)
    if not _is_executable_file(py_path):
        warnings.append(f"report_python_path_not_executable: {python_path}; falling back to PATH python")
        py = shutil.which("python") or shutil.which("python3")
        if py:
            return py, warnings, "path_fallback"
        return None, warnings, None

    return python_path, warnings, "report:python_path"


def _default_assets() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _load_assets_from_prepare(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    data, err = _read_json(path)
    if data is None:
        warnings.append(f"assets_from_missing_or_invalid: {err}")
        return _default_assets(), warnings
    assets = data.get("assets")
    if not isinstance(assets, dict):
        warnings.append("assets_from_missing_assets_object")
        return _default_assets(), warnings
    dataset = assets.get("dataset") if isinstance(assets.get("dataset"), dict) else {}
    model = assets.get("model") if isinstance(assets.get("model"), dict) else {}
    out = _default_assets()
    for key in ("path", "source", "version", "sha256"):
        if isinstance(dataset.get(key), str):
            out["dataset"][key] = dataset.get(key, "")
        if isinstance(model.get(key), str):
            out["model"][key] = model.get(key, "")
    return out, warnings


def _format_command(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(x) for x in argv)


def _substitute_placeholders(argv: List[str], python_exe: Optional[str], repo_root: Path) -> List[str]:
    out: List[str] = []
    for tok in argv:
        if tok in ("{python}", "{{python}}"):
            if python_exe is None:
                out.append(tok)
            else:
                out.append(python_exe)
        elif tok in ("{repo}", "{{repo}}"):
            out.append(str(repo_root))
        else:
            out.append(tok)
    return out


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified executor for env-bench benchmark stages.")
    parser.add_argument("--stage", required=True, help="Stage name (e.g., cpu, single_gpu, multi_gpu).")
    parser.add_argument("--task", required=True, help="Task type (train|infer|check|download|validate|measure).")
    parser.add_argument("--framework", default="unknown", help="Framework (pytorch|tensorflow|jax|unknown).")
    parser.add_argument("--timeout-sec", type=int, default=None, help="Timeout in seconds.")
    parser.add_argument("--out-root", default="build_output", help="Root output directory (default: build_output).")
    parser.add_argument("--python", dest="cli_python", default=None, help="Explicit python executable (highest priority).")
    parser.add_argument("--report-path", default=None, help="Override report path (default: /opt/scimlopsbench/report.json).")
    parser.add_argument("--assets-from", default=None, help="Path to prepare stage results.json to copy assets from.")
    parser.add_argument("--decision-reason", default="", help="Decision rationale to record in results.json meta.")
    parser.add_argument("--failure-category", default=None, help="Override failure_category on failure.")
    parser.add_argument(
        "--skip-reason",
        default=None,
        choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"],
        help="Skip this stage with the given reason (writes results.json, exit 0).",
    )
    parser.add_argument("--skip-message", default="", help="Additional skip message.")
    parser.add_argument(
        "--no-python-required",
        action="store_true",
        help="Do not require report/python resolution; intended for pure shell stages.",
    )
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute (prefix with --).")

    args = parser.parse_args()

    repo_root = _repo_root()
    stage_dir = repo_root / args.out_root / args.stage
    _ensure_dir(stage_dir)

    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    start_time = time.monotonic()
    start_utc = _utc_now_iso()

    status = "failure"
    skip_reason = "unknown"
    exit_code = 1
    failure_category = "unknown"
    command_str = ""
    error_excerpt = ""

    python_resolution_source: Optional[str] = None
    python_warnings: List[str] = []
    resolved_python: Optional[str] = None

    assets_payload: Dict[str, Any] = _default_assets()
    assets_warnings: List[str] = []

    report_path = _resolve_report_path(args.report_path)

    try:
        if args.assets_from:
            assets_payload, assets_warnings = _load_assets_from_prepare(Path(args.assets_from))

        timeout_sec = args.timeout_sec
        if timeout_sec is None:
            timeout_sec = DEFAULT_TIMEOUTS_SEC.get(args.stage, 600)

        if args.skip_reason:
            status = "skipped"
            skip_reason = args.skip_reason
            exit_code = 0
            failure_category = "unknown"
            command_str = "skipped"
            with log_path.open("w", encoding="utf-8") as log_f:
                log_f.write(f"[runner] stage={args.stage} skipped: {args.skip_reason}\n")
                if args.skip_message:
                    log_f.write(f"[runner] message: {args.skip_message}\n")
            error_excerpt = ""
        else:
            if not args.no_python_required:
                resolved_python, python_warnings, python_resolution_source = _resolve_python(
                    args.cli_python, report_path
                )
                if resolved_python is None:
                    status = "failure"
                    exit_code = 1
                    failure_category = "missing_report"
                    with log_path.open("w", encoding="utf-8") as log_f:
                        log_f.write("[runner] Failed to resolve python interpreter.\n")
                        log_f.write(f"[runner] report_path={report_path}\n")
                        for w in python_warnings:
                            log_f.write(f"[runner] warning: {w}\n")
                    error_excerpt = _tail_lines(_safe_read_text(log_path), 220)
                    raise SystemExit(1)

            cmd = args.cmd
            if cmd and cmd[0] == "--":
                cmd = cmd[1:]
            if not cmd:
                status = "failure"
                exit_code = 1
                failure_category = "args_unknown"
                with log_path.open("w", encoding="utf-8") as log_f:
                    log_f.write("[runner] No command provided. Use: runner.py ... -- <cmd>\n")
                error_excerpt = _tail_lines(_safe_read_text(log_path), 220)
                raise SystemExit(1)

            argv = list(cmd)
            argv = _substitute_placeholders(argv, resolved_python, repo_root)
            command_str = _format_command(argv)

            env = os.environ.copy()
            env.setdefault("PYTHONUNBUFFERED", "1")

            with log_path.open("w", encoding="utf-8") as log_f:
                log_f.write(f"[runner] stage={args.stage} task={args.task}\n")
                log_f.write(f"[runner] cwd={repo_root}\n")
                if not args.no_python_required:
                    log_f.write(f"[runner] report_path={report_path}\n")
                    log_f.write(f"[runner] resolved_python={resolved_python} (source={python_resolution_source})\n")
                    for w in python_warnings:
                        log_f.write(f"[runner] python_warning: {w}\n")
                for w in assets_warnings:
                    log_f.write(f"[runner] assets_warning: {w}\n")
                log_f.write(f"[runner] timeout_sec={timeout_sec}\n")
                log_f.write(f"[runner] command={command_str}\n")
                log_f.write("[runner] --- begin command output ---\n")
                log_f.flush()

                try:
                    cp = subprocess.run(
                        argv,
                        cwd=str(repo_root),
                        env=env,
                        stdout=log_f,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=timeout_sec,
                        check=False,
                    )
                    rc = cp.returncode
                except FileNotFoundError as e:
                    rc = 127
                    log_f.write(f"[runner] FileNotFoundError: {e}\n")
                    failure_category = "entrypoint_not_found"
                except subprocess.TimeoutExpired:
                    rc = 124
                    log_f.write(f"[runner] Timeout after {timeout_sec} seconds.\n")
                    failure_category = "timeout"
                except Exception as e:
                    rc = 1
                    log_f.write(f"[runner] Unexpected exception: {type(e).__name__}: {e}\n")
                    failure_category = "runtime"

                log_f.write("\n[runner] --- end command output ---\n")
                log_f.write(f"[runner] return_code={rc}\n")

            log_text = _safe_read_text(log_path)
            error_excerpt = _tail_lines(log_text, 220)

            if rc == 0:
                status = "success"
                exit_code = 0
                failure_category = "unknown"
            else:
                status = "failure"
                exit_code = 1
                if args.failure_category:
                    failure_category = args.failure_category
                elif failure_category == "unknown":
                    if "out of memory" in log_text.lower():
                        failure_category = "oom"
                    else:
                        failure_category = "runtime"
                if args.stage in ("cpu", "single_gpu", "multi_gpu") and failure_category in ("runtime", "unknown"):
                    inferred = _infer_failure_category_from_log(log_text)
                    if inferred:
                        failure_category = inferred

        end_utc = _utc_now_iso()
        duration_sec = time.monotonic() - start_time

        meta: Dict[str, Any] = {
            "python": resolved_python or "",
            "git_commit": _git_commit(repo_root),
            "env_vars": _collect_env_vars(),
            "decision_reason": args.decision_reason,
            "timestamp_utc": end_utc,
            "start_time_utc": start_utc,
            "duration_sec": round(duration_sec, 3),
            "runner": {
                "python_resolution_source": python_resolution_source or "",
                "python_warnings": python_warnings,
                "assets_warnings": assets_warnings,
                "no_python_required": bool(args.no_python_required),
            },
        }

        payload: Dict[str, Any] = {
            "status": status,
            "skip_reason": skip_reason if status == "skipped" else "unknown",
            "exit_code": exit_code,
            "stage": args.stage,
            "task": args.task,
            "command": command_str,
            "timeout_sec": args.timeout_sec if args.timeout_sec is not None else DEFAULT_TIMEOUTS_SEC.get(args.stage, 600),
            "framework": args.framework,
            "assets": assets_payload,
            "meta": meta,
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }

        _write_json(results_path, payload)

        return 0 if exit_code == 0 else 1

    except SystemExit as e:
        # Ensure results.json exists even on early SystemExit.
        try:
            if not results_path.exists():
                meta = {
                    "python": resolved_python or "",
                    "git_commit": _git_commit(repo_root),
                    "env_vars": _collect_env_vars(),
                    "decision_reason": args.decision_reason,
                    "timestamp_utc": _utc_now_iso(),
                    "start_time_utc": start_utc,
                    "duration_sec": round(time.monotonic() - start_time, 3),
                    "runner": {
                        "python_resolution_source": python_resolution_source or "",
                        "python_warnings": python_warnings,
                        "assets_warnings": assets_warnings,
                        "no_python_required": bool(args.no_python_required),
                    },
                }
                payload = {
                    "status": status,
                    "skip_reason": "unknown",
                    "exit_code": exit_code,
                    "stage": args.stage,
                    "task": args.task,
                    "command": command_str or "failed_before_command",
                    "timeout_sec": args.timeout_sec if args.timeout_sec is not None else DEFAULT_TIMEOUTS_SEC.get(args.stage, 600),
                    "framework": args.framework,
                    "assets": assets_payload,
                    "meta": meta,
                    "failure_category": failure_category,
                    "error_excerpt": error_excerpt or _tail_lines(_safe_read_text(log_path), 220),
                }
                _write_json(results_path, payload)
        except Exception:
            pass
        code = int(e.code) if isinstance(e.code, int) else 1
        return 0 if code == 0 else 1

    except Exception as e:
        # Last resort.
        try:
            with log_path.open("a", encoding="utf-8") as log_f:
                log_f.write(f"\n[runner] Fatal error: {type(e).__name__}: {e}\n")
        except Exception:
            pass
        try:
            meta = {
                "python": resolved_python or "",
                "git_commit": _git_commit(repo_root),
                "env_vars": _collect_env_vars(),
                "decision_reason": args.decision_reason,
                "timestamp_utc": _utc_now_iso(),
                "start_time_utc": start_utc,
                "duration_sec": round(time.monotonic() - start_time, 3),
                "runner": {
                    "python_resolution_source": python_resolution_source or "",
                    "python_warnings": python_warnings,
                    "assets_warnings": assets_warnings,
                    "no_python_required": bool(args.no_python_required),
                },
            }
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": args.stage,
                "task": args.task,
                "command": command_str or "failed_before_command",
                "timeout_sec": args.timeout_sec if args.timeout_sec is not None else DEFAULT_TIMEOUTS_SEC.get(args.stage, 600),
                "framework": args.framework,
                "assets": assets_payload,
                "meta": meta,
                "failure_category": failure_category or "unknown",
                "error_excerpt": _tail_lines(_safe_read_text(log_path), 220),
            }
            _write_json(results_path, payload)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
