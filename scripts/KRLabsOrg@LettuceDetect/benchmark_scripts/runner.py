#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


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


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_env_snapshot() -> dict[str, str]:
    whitelist = [
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "XDG_CACHE_HOME",
        "TORCH_HOME",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "HF_TOKEN",
        "HUGGINGFACE_HUB_TOKEN",
        "OPENAI_API_KEY",
    ]
    redacted = {"HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "OPENAI_API_KEY"}
    out: dict[str, str] = {}
    for k in whitelist:
        if k in os.environ:
            out[k] = "***REDACTED***" if k in redacted else os.environ.get(k, "")
    return out


def _git_commit(repo: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            check=False,
            capture_output=True,
            text=True,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    tail = lines[-max_lines:]
    return "\n".join(tail)


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path("/opt/scimlopsbench/report.json")


@dataclass(frozen=True)
class ResolvedPython:
    python: str
    source: str
    warning: str = ""
    report_path: str = ""


def resolve_python(
    *,
    cli_python: str | None,
    requires_python: bool,
    cli_report_path: str | None,
) -> tuple[Optional[ResolvedPython], Optional[str]]:
    """Return (ResolvedPython | None, failure_category | None)."""
    if not requires_python:
        return None, None

    if cli_python:
        return ResolvedPython(python=cli_python, source="cli"), None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return ResolvedPython(python=env_python, source="env:SCIMLOPSBENCH_PYTHON"), None

    report_path = _resolve_report_path(cli_report_path)
    if not report_path.exists():
        return None, "missing_report"
    try:
        report = _read_json(report_path)
    except Exception:
        return None, "missing_report"

    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        return None, "missing_report"

    python_path = python_path.strip()
    rp = Path(python_path)
    if _is_executable_file(rp):
        return ResolvedPython(
            python=python_path, source="report:python_path", report_path=str(report_path)
        ), None

    # Last resort fallback.
    return ResolvedPython(
        python="python",
        source="PATH:fallback",
        warning=f"report python_path is not executable: {python_path!r}; falling back to python from PATH",
        report_path=str(report_path),
    ), None


def _default_timeout(stage: str, cli_timeout: int | None) -> int:
    if cli_timeout is not None:
        return int(cli_timeout)
    return int(DEFAULT_TIMEOUTS_SEC.get(stage, 600))


def _build_assets_payload(
    *,
    assets_from: str | None,
    dataset_path: str | None,
    model_path: str | None,
) -> dict[str, dict[str, str]]:
    base: dict[str, dict[str, str]] = {
        "dataset": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        "model": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
    }
    if assets_from:
        try:
            data = _read_json(Path(assets_from))
            assets = data.get("assets", {})
            if isinstance(assets, dict):
                for k in ("dataset", "model"):
                    if isinstance(assets.get(k), dict):
                        for kk in ("path", "source", "version", "sha256"):
                            vv = assets[k].get(kk)
                            if isinstance(vv, str):
                                base[k][kk] = vv
        except Exception:
            pass

    if dataset_path is not None:
        base["dataset"]["path"] = dataset_path
    if model_path is not None:
        base["model"]["path"] = model_path
    return base


def _write_results(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _shlex_join(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark command runner")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    parser.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    parser.add_argument("--out-dir", default=None, help="Default: build_output/<stage>")
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--skip", action="store_true", help="Skip execution and emit results.json")
    parser.add_argument(
        "--skip-reason",
        default="unknown",
        choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"],
    )
    parser.add_argument("--skip-details", default="")
    parser.add_argument("--failure-category", default="unknown")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python", dest="cli_python", default=None)
    parser.add_argument("--requires-python", action="store_true")
    parser.add_argument("--assets-from", default=None, help="Path to JSON containing an 'assets' object")
    parser.add_argument("--dataset-path", default=None)
    parser.add_argument("--model-path", default=None)
    parser.add_argument("--env", action="append", default=[], help="Extra env vars KEY=VALUE (repeatable)")
    parser.add_argument("--shell", action="store_true", help="Run command via `bash -lc`")
    parser.add_argument("--python-script", default=None, help="Run a python script with resolved python")
    parser.add_argument("--python-module", default=None, help="Run a python module with resolved python")
    parser.add_argument("--python-code", default=None, help="Run python -c CODE with resolved python")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute after --")

    args = parser.parse_args()

    repo = _repo_root()
    stage = args.stage
    out_dir = Path(args.out_dir) if args.out_dir else (repo / "build_output" / stage)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = _default_timeout(stage, args.timeout_sec)

    requires_python = bool(args.requires_python or args.python_script or args.python_module or args.python_code)
    resolved_python, python_failure_category = resolve_python(
        cli_python=args.cli_python, requires_python=requires_python, cli_report_path=args.report_path
    )

    assets_payload = _build_assets_payload(
        assets_from=args.assets_from, dataset_path=args.dataset_path, model_path=args.model_path
    )

    base_meta: dict[str, Any] = {
        "timestamp_utc": _utc_timestamp(),
        "git_commit": _git_commit(repo),
        "env_vars": _safe_env_snapshot(),
        "decision_reason": args.decision_reason,
        "python": "",
        "runner_python": sys.executable,
    }
    if resolved_python:
        base_meta["python"] = resolved_python.python
        base_meta["resolved_python"] = resolved_python.python
        base_meta["resolved_python_source"] = resolved_python.source
        if resolved_python.warning:
            base_meta["resolved_python_warning"] = resolved_python.warning
        if resolved_python.report_path:
            base_meta["report_path"] = resolved_python.report_path
    else:
        base_meta["python"] = sys.executable

    # Pre-build command.
    command_list: list[str] = []
    command_display = ""

    if args.skip:
        command_display = "<skipped>"
    elif python_failure_category is not None:
        command_display = "<python_resolution_failed>"
    else:
        if args.python_script:
            if not resolved_python:
                command_display = "<python_missing>"
            else:
                command_list = [resolved_python.python, args.python_script]
                if args.cmd and args.cmd[0] == "--":
                    command_list.extend(args.cmd[1:])
                else:
                    command_list.extend(args.cmd)
        elif args.python_module:
            if not resolved_python:
                command_display = "<python_missing>"
            else:
                command_list = [resolved_python.python, "-m", args.python_module]
                if args.cmd and args.cmd[0] == "--":
                    command_list.extend(args.cmd[1:])
                else:
                    command_list.extend(args.cmd)
        elif args.python_code is not None:
            if not resolved_python:
                command_display = "<python_missing>"
            else:
                command_list = [resolved_python.python, "-c", args.python_code]
        else:
            # Generic command.
            if args.cmd and args.cmd[0] == "--":
                command_list = args.cmd[1:]
            else:
                command_list = args.cmd

        command_display = (
            _shlex_join(command_list) if command_list else "<missing_command>"
        )

    # Start log.
    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"stage={stage}\n")
        logf.write(f"task={args.task}\n")
        logf.write(f"timeout_sec={timeout_sec}\n")
        if args.decision_reason:
            logf.write(f"decision_reason={args.decision_reason}\n")
        logf.write(f"command={command_display}\n")
        if resolved_python and resolved_python.warning:
            logf.write(f"warning={resolved_python.warning}\n")
        if args.skip:
            logf.write(f"skipped=true reason={args.skip_reason} details={args.skip_details}\n")

    status = "failure"
    stage_exit_code = 1
    failure_category = args.failure_category
    raw_exit_code: Optional[int] = None
    elapsed_sec: Optional[float] = None

    if args.skip:
        status = "skipped"
        stage_exit_code = 0
        failure_category = "unknown"
    elif python_failure_category is not None:
        status = "failure"
        stage_exit_code = 1
        failure_category = python_failure_category
    elif not command_list:
        status = "failure"
        stage_exit_code = 1
        failure_category = "entrypoint_not_found"
    else:
        env = os.environ.copy()
        for item in args.env:
            if "=" not in item:
                continue
            k, v = item.split("=", 1)
            env[k] = v

        start = time.time()
        try:
            with log_path.open("a", encoding="utf-8") as logf:
                logf.write("\n--- runner output ---\n")
                if args.shell:
                    shell_cmd = command_display
                    raw_exit_code = subprocess.run(
                        ["bash", "-lc", shell_cmd],
                        cwd=str(repo),
                        env=env,
                        stdout=logf,
                        stderr=logf,
                        timeout=timeout_sec,
                        check=False,
                        text=True,
                    ).returncode
                else:
                    raw_exit_code = subprocess.run(
                        command_list,
                        cwd=str(repo),
                        env=env,
                        stdout=logf,
                        stderr=logf,
                        timeout=timeout_sec,
                        check=False,
                        text=True,
                    ).returncode
        except subprocess.TimeoutExpired:
            raw_exit_code = 124
            failure_category = "timeout"
        except FileNotFoundError:
            raw_exit_code = 127
            failure_category = "entrypoint_not_found"
        except Exception:
            raw_exit_code = 1
            failure_category = "unknown"
        finally:
            elapsed_sec = time.time() - start

        if failure_category == "timeout":
            status = "failure"
            stage_exit_code = 1
        elif raw_exit_code == 0:
            status = "success"
            stage_exit_code = 0
            failure_category = "unknown"
        else:
            status = "failure"
            stage_exit_code = 1
            if failure_category == "unknown":
                failure_category = "runtime"

    payload: dict[str, Any] = {
        "status": status,
        "skip_reason": args.skip_reason if status == "skipped" else "unknown",
        "exit_code": int(stage_exit_code),
        "stage": stage,
        "task": args.task,
        "command": command_display,
        "timeout_sec": int(timeout_sec),
        "framework": args.framework,
        "assets": assets_payload,
        "meta": {
            **base_meta,
            "elapsed_sec": elapsed_sec,
            "command_exit_code": raw_exit_code,
        },
        "failure_category": failure_category,
        "error_excerpt": _tail_text(log_path, max_lines=220),
    }

    _write_results(results_path, payload)
    return stage_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
