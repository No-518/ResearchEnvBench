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
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, f"Expected JSON object in {path}"
        return data, None
    except FileNotFoundError:
        return None, f"Missing file: {path}"
    except Exception as exc:  # noqa: BLE001
        return None, f"Failed to read JSON from {path}: {exc}"


def _tail_file(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[-max_lines:])


def _get_git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).strip()
        return out
    except Exception:  # noqa: BLE001
        return ""


def _report_path(cli_report_path: Optional[str]) -> str:
    if cli_report_path:
        return cli_report_path
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return env
    return DEFAULT_REPORT_PATH


def _is_executable_file(path: str) -> bool:
    try:
        p = Path(path)
        return p.is_file() and os.access(p, os.X_OK)
    except Exception:  # noqa: BLE001
        return False


def _resolve_python(
    *,
    cli_python: Optional[str],
    require_python: bool,
    cli_report_path: Optional[str],
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """
    Returns: (python_path, meta, failure_category_if_any)
    """
    meta: Dict[str, Any] = {"resolution": [], "warnings": []}

    if cli_python:
        meta["resolution"].append({"source": "cli", "python": cli_python})
        if _is_executable_file(cli_python):
            return cli_python, meta, None
        return None, meta, "path_hallucination"

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        meta["resolution"].append({"source": "env:SCIMLOPSBENCH_PYTHON", "python": env_python})
        if _is_executable_file(env_python):
            return env_python, meta, None
        meta["warnings"].append("SCIMLOPSBENCH_PYTHON is set but is not an executable file; falling back.")

    rp = Path(_report_path(cli_report_path))
    report, report_err = _safe_read_json(rp)
    meta["report_path"] = str(rp)
    if report is None:
        if require_python:
            meta["warnings"].append(report_err or "missing/invalid report")
            return None, meta, "missing_report"
        return None, meta, None

    python_path = report.get("python_path")
    meta["resolution"].append({"source": "report.json:python_path", "python": python_path})
    if isinstance(python_path, str) and python_path:
        if _is_executable_file(python_path):
            return python_path, meta, None
        meta["warnings"].append("report.json python_path is not an executable file; attempting PATH fallback.")
    else:
        if require_python:
            meta["warnings"].append("report.json missing python_path")
            return None, meta, "missing_report"

    fallback = shutil.which("python3") or shutil.which("python")
    if fallback:
        meta["resolution"].append({"source": "PATH", "python": fallback})
        meta["warnings"].append("Using fallback python from PATH (report python_path missing/invalid).")
        return fallback, meta, None

    if require_python:
        meta["warnings"].append("No python executable found on PATH.")
        return None, meta, "missing_report"
    return None, meta, None


def _python_version(python_exe: str) -> str:
    try:
        out = subprocess.check_output(
            [python_exe, "-c", "import platform; print(platform.python_version())"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        ).strip()
        return out
    except Exception:  # noqa: BLE001
        return ""


def _load_assets_from(path: Optional[str]) -> Dict[str, Any]:
    unknown = {"path": "", "source": "", "version": "", "sha256": ""}
    if not path:
        return {"dataset": dict(unknown), "model": dict(unknown)}
    p = Path(path)
    data, err = _safe_read_json(p)
    if data is None:
        return {"dataset": dict(unknown), "model": dict(unknown)}
    assets = data.get("assets")
    if not isinstance(assets, dict):
        return {"dataset": dict(unknown), "model": dict(unknown)}
    ds = assets.get("dataset") if isinstance(assets.get("dataset"), dict) else {}
    md = assets.get("model") if isinstance(assets.get("model"), dict) else {}
    out = {
        "dataset": {
            "path": str(ds.get("path", "")),
            "source": str(ds.get("source", "")),
            "version": str(ds.get("version", "")),
            "sha256": str(ds.get("sha256", "")),
        },
        "model": {
            "path": str(md.get("path", "")),
            "source": str(md.get("source", "")),
            "version": str(md.get("version", "")),
            "sha256": str(md.get("sha256", "")),
        },
    }
    return out


def _classify_failure_from_log(log_tail: str) -> str:
    lt = log_tail.lower()
    if "no such file or directory" in lt or "not found" in lt and "command" in lt:
        return "entrypoint_not_found"
    if "unrecognized arguments" in lt or "unknown argument" in lt or "invalid option" in lt:
        return "args_unknown"
    if "authentication" in lt or "401" in lt or "403" in lt or "token" in lt and "huggingface" in lt:
        return "auth_required"
    if "no module named" in lt or "moduleNotFoundError".lower() in lt:
        return "deps"
    if "cuda out of memory" in lt or "out of memory" in lt:
        return "oom"
    return "runtime"


def _write_results(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified stage runner for scimlopsbench-style benchmarks.")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    parser.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    parser.add_argument("--timeout-sec", type=int, default=600)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--assets-from", default="")
    parser.add_argument("--report-path", default="")

    parser.add_argument("--python", dest="python_exe", default="")
    parser.add_argument("--require-python", action="store_true")
    parser.add_argument("--python-script", default="")
    parser.add_argument("--python-module", default="")

    parser.add_argument("--env", action="append", default=[], help="KEY=VALUE environment override (repeatable)")

    parser.add_argument("--skip", action="store_true")
    parser.add_argument("--skip-reason", default="unknown", choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"])

    parser.add_argument("--no-run", action="store_true", help="Do not execute a command; only emit results.json/log.txt")
    parser.add_argument("--status", default="", choices=["", "success", "failure", "skipped"])
    parser.add_argument("--failure-category", default="", help="Override failure_category (optional)")
    parser.add_argument("--command-str", default="", help="Command string to record when --no-run is used.")

    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run; prefix with --")
    args = parser.parse_args(argv)

    repo_root = _repo_root()

    out_dir = Path(args.out_dir) if args.out_dir else repo_root / "build_output" / args.stage
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    assets = _load_assets_from(args.assets_from) if args.assets_from else _load_assets_from(None)

    env = os.environ.copy()
    env_updates: Dict[str, str] = {}
    for kv in args.env:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        env_updates[k] = v
        env[k] = v

    resolved_python, py_meta, py_fail_cat = _resolve_python(
        cli_python=args.python_exe or None,
        require_python=args.require_python or bool(args.python_script or args.python_module),
        cli_report_path=args.report_path or None,
    )

    # Determine command list
    cmd_list: List[str] = []
    if args.skip:
        status = "skipped"
        skip_reason = args.skip_reason
        stage_exit_code = 0
        command_str = args.command_str or ""
        failure_category = "unknown"
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"Stage skipped: {skip_reason}\n")
            if args.decision_reason:
                f.write(f"Decision: {args.decision_reason}\n")
        payload = {
            "status": status,
            "skip_reason": skip_reason,
            "exit_code": stage_exit_code,
            "stage": args.stage,
            "task": args.task,
            "command": command_str,
            "timeout_sec": int(args.timeout_sec),
            "framework": args.framework,
            "assets": assets,
            "meta": {
                "python": _python_version(resolved_python) if resolved_python else "",
                "python_executable": resolved_python or "",
                "python_resolution": py_meta,
                "git_commit": _get_git_commit(repo_root),
                "env_vars": {k: env.get(k, "") for k in sorted(set(env_updates.keys()))},
                "decision_reason": args.decision_reason,
                "timestamp_utc": _utc_timestamp(),
            },
            "failure_category": failure_category,
            "error_excerpt": "",
        }
        _write_results(results_path, payload)
        return stage_exit_code

    if args.no_run:
        status = args.status or "failure"
        skip_reason = args.skip_reason if status == "skipped" else "not_applicable"
        stage_exit_code = 0 if status in ("success", "skipped") else 1
        failure_category = args.failure_category or ("unknown" if status != "failure" else "runtime")
        command_str = args.command_str or ""
        with log_path.open("w", encoding="utf-8") as f:
            f.write("No-run mode.\n")
            if command_str:
                f.write(f"Recorded command: {command_str}\n")
            if args.decision_reason:
                f.write(f"Decision: {args.decision_reason}\n")
        payload = {
            "status": status,
            "skip_reason": skip_reason,
            "exit_code": stage_exit_code,
            "stage": args.stage,
            "task": args.task,
            "command": command_str,
            "timeout_sec": int(args.timeout_sec),
            "framework": args.framework,
            "assets": assets,
            "meta": {
                "python": _python_version(resolved_python) if resolved_python else "",
                "python_executable": resolved_python or "",
                "python_resolution": py_meta,
                "git_commit": _get_git_commit(repo_root),
                "env_vars": {k: env.get(k, "") for k in sorted(set(env_updates.keys()))},
                "decision_reason": args.decision_reason,
                "timestamp_utc": _utc_timestamp(),
            },
            "failure_category": failure_category,
            "error_excerpt": "",
        }
        _write_results(results_path, payload)
        return stage_exit_code

    if args.python_script or args.python_module:
        if resolved_python is None:
            with log_path.open("w", encoding="utf-8") as f:
                f.write("Failed to resolve python interpreter.\n")
                if py_fail_cat:
                    f.write(f"failure_category={py_fail_cat}\n")
                f.write(json.dumps(py_meta, ensure_ascii=False, indent=2) + "\n")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": args.stage,
                "task": args.task,
                "command": "",
                "timeout_sec": int(args.timeout_sec),
                "framework": args.framework,
                "assets": assets,
                "meta": {
                    "python": "",
                    "python_executable": "",
                    "python_resolution": py_meta,
                    "git_commit": _get_git_commit(repo_root),
                    "env_vars": {k: env.get(k, "") for k in sorted(set(env_updates.keys()))},
                    "decision_reason": args.decision_reason,
                    "timestamp_utc": _utc_timestamp(),
                },
                "failure_category": py_fail_cat or "missing_report",
                "error_excerpt": _tail_file(log_path),
            }
            _write_results(results_path, payload)
            return 1

        if args.python_script and args.python_module:
            raise SystemExit("Provide only one of --python-script or --python-module")

        cmd_list = [resolved_python]
        if args.python_module:
            cmd_list += ["-m", args.python_module]
        else:
            cmd_list += [args.python_script]
        if args.command and args.command[0] == "--":
            cmd_list += args.command[1:]
        else:
            cmd_list += args.command
    else:
        if not args.command:
            raise SystemExit("Missing command. Provide a command after '--', or use --python-script/--python-module.")
        cmd_list = args.command[1:] if args.command[0] == "--" else args.command

    command_str = " ".join(shlex.quote(x) for x in cmd_list)

    # Run
    start = time.time()
    command_returncode: Optional[int] = None
    timed_out = False

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[runner] repo_root={repo_root}\n")
        log_f.write(f"[runner] stage={args.stage} task={args.task}\n")
        log_f.write(f"[runner] command={command_str}\n")
        log_f.write(f"[runner] timeout_sec={args.timeout_sec}\n")
        if args.decision_reason:
            log_f.write(f"[runner] decision_reason={args.decision_reason}\n")
        log_f.write("\n")
        log_f.flush()

        try:
            completed = subprocess.run(
                cmd_list,
                cwd=str(repo_root),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=args.timeout_sec,
            )
            command_returncode = int(completed.returncode)
        except subprocess.TimeoutExpired:
            timed_out = True
            log_f.write("\n[runner] TIMEOUT\n")
        except FileNotFoundError as exc:
            log_f.write(f"\n[runner] FileNotFoundError: {exc}\n")
        except Exception as exc:  # noqa: BLE001
            log_f.write(f"\n[runner] Exception: {exc}\n")

    duration_sec = time.time() - start
    log_tail = _tail_file(log_path)

    if timed_out:
        status = "failure"
        stage_exit_code = 1
        failure_category = "timeout"
    elif command_returncode == 0:
        status = "success"
        stage_exit_code = 0
        failure_category = "unknown"
    else:
        status = "failure"
        stage_exit_code = 1
        failure_category = _classify_failure_from_log(log_tail)

    if args.failure_category:
        failure_category = args.failure_category

    meta_env_keys = {
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_HUB_OFFLINE",
        "HF_HUB_DISABLE_TELEMETRY",
        "TRANSFORMERS_CACHE",
        "HF_HUB_CACHE",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "HOME",
        "TMPDIR",
    }
    meta_env = {k: env.get(k, "") for k in sorted(meta_env_keys.union(env_updates.keys())) if env.get(k) is not None}

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": stage_exit_code,
        "stage": args.stage,
        "task": args.task,
        "command": command_str,
        "timeout_sec": int(args.timeout_sec),
        "framework": args.framework,
        "assets": assets,
        "meta": {
            "python": _python_version(resolved_python) if resolved_python else "",
            "python_executable": resolved_python or "",
            "python_resolution": py_meta,
            "git_commit": _get_git_commit(repo_root),
            "env_vars": meta_env,
            "decision_reason": args.decision_reason,
            "duration_sec": round(duration_sec, 3),
            "command_returncode": command_returncode,
            "timestamp_utc": _utc_timestamp(),
        },
        "failure_category": failure_category,
        "error_excerpt": log_tail if status == "failure" else "",
    }

    _write_results(results_path, payload)
    return stage_exit_code


if __name__ == "__main__":
    raise SystemExit(main())

