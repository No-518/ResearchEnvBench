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


def _repo_root() -> Path:
    # benchmark_scripts/runner.py -> repo root
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:  # noqa: BLE001
        return None, f"invalid_json: {e}"


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return DEFAULT_REPORT_PATH


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except Exception:
        return False


def resolve_python_interpreter(
    *,
    cli_python: Optional[str],
    report_path: Path,
    require_python: bool,
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """
    Priority:
      1) CLI --python
      2) env SCIMLOPSBENCH_PYTHON
      3) report.json["python_path"]
      4) python from PATH (record warning)
    """
    meta: Dict[str, Any] = {
        "resolved_from": None,
        "warnings": [],
        "report_path": str(report_path),
    }

    if cli_python:
        meta["resolved_from"] = "cli"
        return cli_python, meta, None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        meta["resolved_from"] = "env:SCIMLOPSBENCH_PYTHON"
        return env_python, meta, None

    report, report_err = _read_json(report_path)
    if report is None:
        if require_python:
            meta["resolved_from"] = "report"
            meta["warnings"].append(f"report_unavailable: {report_err}")
            return None, meta, "missing_report"
    else:
        python_path = report.get("python_path")
        if isinstance(python_path, str) and python_path:
            meta["resolved_from"] = "report:python_path"
            return python_path, meta, None
        if require_python:
            meta["resolved_from"] = "report"
            meta["warnings"].append("python_path_missing_in_report")
            return None, meta, "missing_report"

    fallback = shutil.which("python3") or shutil.which("python")
    if fallback:
        meta["resolved_from"] = "path_fallback"
        meta["warnings"].append("fell_back_to_python_from_PATH")
        return fallback, meta, None

    if require_python:
        meta["resolved_from"] = "none"
        return None, meta, "missing_report"
    meta["resolved_from"] = "none"
    return None, meta, None


def _git_commit(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        return out or None
    except Exception:
        return None


def _env_snapshot() -> Dict[str, str]:
    keys = [
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "SCIMLOPSBENCH_DATASET_PATH",
        "SCIMLOPSBENCH_MODEL_PATH",
        "SCIMLOPSBENCH_DATASET_URL",
        "SCIMLOPSBENCH_MODEL_ID",
        "SCIMLOPSBENCH_MODEL_REVISION",
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_TOKEN",
        "TRANSFORMERS_CACHE",
        "XDG_CACHE_HOME",
    ]
    out: Dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            out[k] = v
    return out


def _tail_text_lines(path: Path, max_lines: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = text.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)


def _load_assets_manifest(repo_root: Path) -> Dict[str, Any]:
    manifest_path = repo_root / "benchmark_assets" / "manifest.json"
    data, err = _read_json(manifest_path)
    if data is None:
        return {
            "dataset": {
                "path": "",
                "source": "",
                "version": "",
                "sha256": "",
            },
            "model": {
                "path": "",
                "source": "",
                "version": "",
                "sha256": "",
            },
            "_manifest_error": err,
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


def _cmd_to_string(argv: List[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def _write_results(path: Path, payload: Dict[str, Any]) -> None:
    _safe_mkdir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_stage_command(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    stage: str = args.stage
    task: str = args.task
    framework: str = args.framework

    out_dir = Path(args.out_dir) if args.out_dir else (repo_root / "build_output" / stage)
    if not out_dir.is_absolute():
        out_dir = (repo_root / out_dir).resolve()
    _safe_mkdir(out_dir)

    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = int(args.timeout_sec) if args.timeout_sec is not None else int(
        DEFAULT_TIMEOUTS_SEC.get(stage, 600)
    )

    report_path = _resolve_report_path(args.report_path)
    require_python = any(a == "{python}" for a in args.command)
    resolved_python, python_meta, python_failure = resolve_python_interpreter(
        cli_python=args.python,
        report_path=report_path,
        require_python=require_python,
    )

    assets = _load_assets_manifest(repo_root)
    git_commit = _git_commit(repo_root) or ""

    def finalize(
        *,
        status: str,
        exit_code: int,
        command_str: str,
        skip_reason: str,
        failure_category: str,
        command_return_code: Optional[int],
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        meta: Dict[str, Any] = {
            "python": resolved_python or "",
            "git_commit": git_commit,
            "env_vars": _env_snapshot(),
            "decision_reason": args.decision_reason or "",
            "timestamp_utc": _utc_timestamp(),
            "python_resolution": python_meta,
        }
        if extra_meta:
            meta.update(extra_meta)
        if command_return_code is not None:
            meta["command_return_code"] = command_return_code

        payload: Dict[str, Any] = {
            "status": status,
            "skip_reason": skip_reason,
            "exit_code": exit_code,
            "stage": stage,
            "task": task,
            "command": command_str,
            "timeout_sec": timeout_sec,
            "framework": framework,
            "assets": {
                "dataset": assets.get("dataset", {}),
                "model": assets.get("model", {}),
            },
            "meta": meta,
            "failure_category": failure_category,
            "error_excerpt": _tail_text_lines(log_path, 220),
        }
        _write_results(results_path, payload)
        return 0 if status in ("success", "skipped") else 1

    if args.skip:
        skip_reason = args.skip_reason or "unknown"
        with log_path.open("w", encoding="utf-8") as log_f:
            log_f.write(f"[runner] stage={stage} skipped reason={skip_reason}\n")
        return finalize(
            status="skipped",
            exit_code=0,
            command_str=args.skip_command or "",
            skip_reason=skip_reason,
            failure_category="unknown",
            command_return_code=None,
        )

    if python_failure is not None:
        with log_path.open("w", encoding="utf-8") as log_f:
            log_f.write(
                f"[runner] failed to resolve python (required={require_python}). "
                f"failure={python_failure} report_path={report_path}\n"
            )
        return finalize(
            status="failure",
            exit_code=1,
            command_str=_cmd_to_string(args.command),
            skip_reason="unknown",
            failure_category="missing_report",
            command_return_code=None,
        )

    # Replace "{python}" placeholders.
    final_cmd = [resolved_python if a == "{python}" else a for a in args.command]
    command_str = _cmd_to_string(final_cmd)

    command_return_code: Optional[int] = None
    failure_category = "unknown"
    stage_status = "failure"

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[runner] repo_root={repo_root}\n")
        log_f.write(f"[runner] stage={stage} task={task}\n")
        log_f.write(f"[runner] command={command_str}\n")
        log_f.write(f"[runner] timeout_sec={timeout_sec}\n")
        log_f.write(f"[runner] start_utc={_utc_timestamp()}\n")
        log_f.flush()

        try:
            proc = subprocess.Popen(
                final_cmd,
                cwd=str(repo_root),
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
                env=os.environ.copy(),
            )
        except FileNotFoundError as e:
            log_f.write(f"[runner] entrypoint_not_found: {e}\n")
            return finalize(
                status="failure",
                exit_code=1,
                command_str=command_str,
                skip_reason="unknown",
                failure_category="entrypoint_not_found",
                command_return_code=None,
            )
        except Exception as e:  # noqa: BLE001
            log_f.write(f"[runner] spawn_failed: {e}\n")
            return finalize(
                status="failure",
                exit_code=1,
                command_str=command_str,
                skip_reason="unknown",
                failure_category="runtime",
                command_return_code=None,
            )

        try:
            proc.wait(timeout=timeout_sec)
            command_return_code = proc.returncode
        except subprocess.TimeoutExpired:
            log_f.write(f"[runner] timeout after {timeout_sec}s, terminating...\n")
            failure_category = "timeout"
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=15)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            command_return_code = proc.returncode

    if command_return_code == 0:
        stage_status = "success"
        failure_category = "unknown"
    else:
        stage_status = "failure"
        if failure_category == "unknown":
            failure_category = "runtime"

    return finalize(
        status=stage_status,
        exit_code=0 if stage_status == "success" else 1,
        command_str=command_str,
        skip_reason="not_applicable" if stage_status == "success" else "unknown",
        failure_category=failure_category,
        command_return_code=command_return_code,
    )


def resolve_python_cmd(args: argparse.Namespace) -> int:
    report_path = _resolve_report_path(args.report_path)
    resolved_python, _meta, failure = resolve_python_interpreter(
        cli_python=args.python,
        report_path=report_path,
        require_python=True,
    )
    if failure is not None or not resolved_python:
        print(f"ERROR: unable to resolve python (failure={failure})", file=sys.stderr)
        return 1
    print(resolved_python)
    return 0


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified benchmark command runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run a command and write log/results JSON")
    p_run.add_argument("--stage", required=True)
    p_run.add_argument("--task", required=True)
    p_run.add_argument("--framework", default="unknown")
    p_run.add_argument("--out-dir", default=None)
    p_run.add_argument("--timeout-sec", type=int, default=None)
    p_run.add_argument("--decision-reason", default=None)
    p_run.add_argument("--python", default=None)
    p_run.add_argument("--report-path", default=None)
    p_run.add_argument("--skip", action="store_true")
    p_run.add_argument("--skip-reason", default=None)
    p_run.add_argument("--skip-command", default=None)
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")

    p_resolve = sub.add_parser("resolve-python", help="Print resolved python path")
    p_resolve.add_argument("--python", default=None)
    p_resolve.add_argument("--report-path", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "run":
        if not args.command or args.command[0] != "--":
            parser.error("runner run requires command after --")
        args.command = args.command[1:]

    return args


def main(argv: List[str]) -> int:
    args = _parse_args(argv)
    if args.cmd == "resolve-python":
        return resolve_python_cmd(args)
    if args.cmd == "run":
        return run_stage_command(args)
    print("ERROR: unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

