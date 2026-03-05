#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text_tail(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = text.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)


def _write_json(path: Path, payload: Any) -> None:
    _safe_mkdir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def _load_report_json(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data, None
        return None, "report_json_not_object"
    except FileNotFoundError:
        return None, "report_missing"
    except Exception:
        return None, "report_invalid_json"


def _is_executable(path: Path) -> bool:
    try:
        return path.exists() and os.access(str(path), os.X_OK) and path.is_file()
    except Exception:
        return False


@dataclass
class PythonResolution:
    python: str
    source: str  # cli | env | report | path_fallback
    warnings: List[str]
    report_path: str
    report_python_path: Optional[str]


def resolve_python(
    *,
    cli_python: Optional[str],
    report_path: Path,
) -> Tuple[Optional[PythonResolution], Optional[str]]:
    """
    Returns (resolution, failure_category) where failure_category is only set when resolution is None.

    Rules:
    - CLI --python wins
    - SCIMLOPSBENCH_PYTHON wins next
    - Else, try report.json python_path (requires report file unless require_report_file=False)
    - Else, fallback python from PATH (record warning) if report exists but python_path missing/invalid
    """
    warnings: List[str] = []

    if cli_python:
        return (
            PythonResolution(
                python=cli_python,
                source="cli",
                warnings=warnings,
                report_path=str(report_path),
                report_python_path=None,
            ),
            None,
        )

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return (
            PythonResolution(
                python=env_python,
                source="env",
                warnings=warnings,
                report_path=str(report_path),
                report_python_path=None,
            ),
            None,
        )

    report, report_err = _load_report_json(report_path)
    if report is None:
        return None, "missing_report"

    report_python_path = report.get("python_path")
    if isinstance(report_python_path, str) and report_python_path.strip():
        candidate = Path(report_python_path)
        if _is_executable(candidate):
            return (
                PythonResolution(
                    python=str(candidate),
                    source="report",
                    warnings=warnings,
                    report_path=str(report_path),
                    report_python_path=report_python_path,
                ),
                None,
            )
        warnings.append("report_python_path_not_executable")
    else:
        warnings.append("report_python_path_missing_or_empty")

    fallback = shutil.which("python") or shutil.which("python3")
    if not fallback:
        return None, "missing_report"
    warnings.append("using_fallback_python_from_PATH")
    return (
        PythonResolution(
            python=fallback,
            source="path_fallback",
            warnings=warnings,
            report_path=str(report_path),
            report_python_path=report_python_path if isinstance(report_python_path, str) else None,
        ),
        None,
    )


def default_assets_payload() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def load_assets_from_results(results_path: Path) -> Dict[str, Dict[str, str]]:
    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
        assets = data.get("assets")
        if isinstance(assets, dict):
            dataset = assets.get("dataset") if isinstance(assets.get("dataset"), dict) else {}
            model = assets.get("model") if isinstance(assets.get("model"), dict) else {}
            merged = default_assets_payload()
            for k in ("path", "source", "version", "sha256"):
                if isinstance(dataset.get(k), str):
                    merged["dataset"][k] = dataset[k]
                if isinstance(model.get(k), str):
                    merged["model"][k] = model[k]
            return merged
    except Exception:
        pass
    return default_assets_payload()


def build_base_results(
    *,
    stage: str,
    task: str,
    command_str: str,
    timeout_sec: int,
    framework: str,
    exit_code: int,
    status: str,
    skip_reason: str,
    assets: Dict[str, Dict[str, str]],
    meta: Dict[str, Any],
    failure_category: str,
    error_excerpt: str,
) -> Dict[str, Any]:
    return {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": int(exit_code),
        "stage": stage,
        "task": task,
        "command": command_str,
        "timeout_sec": int(timeout_sec),
        "framework": framework,
        "assets": assets,
        "meta": meta,
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }


def run_command_capture(
    *,
    cmd: Sequence[str],
    cwd: Path,
    env: Dict[str, str],
    timeout_sec: int,
    log_path: Path,
) -> Tuple[int, bool]:
    _safe_mkdir(log_path.parent)
    with log_path.open("w", encoding="utf-8", errors="replace") as log_f:
        log_f.write(f"[runner] timestamp_utc={_utc_timestamp()}\n")
        log_f.write(f"[runner] cwd={cwd}\n")
        log_f.write(f"[runner] cmd={shlex.join(list(cmd))}\n")
        log_f.flush()

        proc = subprocess.Popen(
            list(cmd),
            cwd=str(cwd),
            env=env,
            stdout=log_f,
            stderr=log_f,
            text=True,
            start_new_session=True,
        )
        timed_out = False
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        return int(proc.returncode if proc.returncode is not None else 1), timed_out


def _parse_env_kv(pairs: Optional[List[str]]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not pairs:
        return env
    for item in pairs:
        if "=" not in item:
            raise ValueError(f"Invalid --env value (expected KEY=VALUE): {item!r}")
        k, v = item.split("=", 1)
        env[k] = v
    return env


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="runner.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Unified benchmark command runner that writes build_output/<stage>/{log.txt,results.json}.",
        epilog=textwrap.dedent(
            """\
            Example:
              python benchmark_scripts/runner.py run --stage cpu --task train --framework pytorch --timeout-sec 600 \\
                --env PYTHONPATH=end-to-end -- \\
                {python} end-to-end/main_train_drunet.py --opt build_output/cpu/options.json
            """
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run a command and write results.json")
    run_p.add_argument("--stage", required=True)
    run_p.add_argument("--task", required=True)
    run_p.add_argument("--framework", default="unknown")
    run_p.add_argument("--timeout-sec", type=int, required=True)
    run_p.add_argument("--python", dest="cli_python")
    run_p.add_argument("--report-path")
    run_p.add_argument("--assets-from", help="Path to another stage's results.json to copy assets from.")
    run_p.add_argument("--decision-reason", default="")
    run_p.add_argument("--meta-json", help="JSON file merged into meta (dict).")
    run_p.add_argument("--env", action="append", help="Environment variable KEY=VALUE (repeatable).")
    run_p.add_argument("command", nargs=argparse.REMAINDER, help="Command to run (use -- then tokens).")

    skip_p = sub.add_parser("skip", help="Write a skipped results.json (no command executed)")
    skip_p.add_argument("--stage", required=True)
    skip_p.add_argument("--task", required=True)
    skip_p.add_argument("--framework", default="unknown")
    skip_p.add_argument("--timeout-sec", type=int, default=0)
    skip_p.add_argument("--skip-reason", required=True)
    skip_p.add_argument("--command", default="")
    skip_p.add_argument("--assets-from")
    skip_p.add_argument("--decision-reason", default="")
    skip_p.add_argument("--meta-json")

    fail_p = sub.add_parser("fail", help="Write a failure results.json (no command executed)")
    fail_p.add_argument("--stage", required=True)
    fail_p.add_argument("--task", required=True)
    fail_p.add_argument("--framework", default="unknown")
    fail_p.add_argument("--timeout-sec", type=int, default=0)
    fail_p.add_argument("--failure-category", required=True)
    fail_p.add_argument("--command", default="")
    fail_p.add_argument("--assets-from")
    fail_p.add_argument("--decision-reason", default="")
    fail_p.add_argument("--meta-json")
    fail_p.add_argument("--error-message", default="")

    args = parser.parse_args(list(argv) if argv is not None else None)

    os.chdir(REPO_ROOT)

    stage = args.stage
    out_dir = REPO_ROOT / "build_output" / stage
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    assets = default_assets_payload()
    if getattr(args, "assets_from", None):
        assets = load_assets_from_results(Path(args.assets_from))

    meta_extra: Dict[str, Any] = {}
    if getattr(args, "meta_json", None):
        try:
            meta_extra = json.loads(Path(args.meta_json).read_text(encoding="utf-8"))
            if not isinstance(meta_extra, dict):
                meta_extra = {"_meta_json_warning": "meta_json_not_object"}
        except Exception as e:
            meta_extra = {"_meta_json_warning": f"meta_json_load_failed:{type(e).__name__}"}

    base_meta: Dict[str, Any] = {
        "python": "",
        "git_commit": _git_commit(REPO_ROOT),
        "env_vars": {},
        "decision_reason": getattr(args, "decision_reason", ""),
        "timestamp_utc": _utc_timestamp(),
    }
    base_meta.update(meta_extra)

    if args.cmd == "skip":
        base_meta["python"] = sys.executable
        payload = build_base_results(
            stage=stage,
            task=args.task,
            command_str=args.command,
            timeout_sec=int(args.timeout_sec),
            framework=args.framework,
            exit_code=0,
            status="skipped",
            skip_reason=args.skip_reason,
            assets=assets,
            meta=base_meta,
            failure_category="not_applicable",
            error_excerpt="",
        )
        _safe_mkdir(out_dir)
        (out_dir / "log.txt").write_text(
            f"[runner] skipped stage={stage} reason={args.skip_reason} timestamp_utc={_utc_timestamp()}\n",
            encoding="utf-8",
        )
        _write_json(results_path, payload)
        return 0

    if args.cmd == "fail":
        base_meta["python"] = sys.executable
        payload = build_base_results(
            stage=stage,
            task=args.task,
            command_str=args.command,
            timeout_sec=int(args.timeout_sec),
            framework=args.framework,
            exit_code=1,
            status="failure",
            skip_reason="not_applicable",
            assets=assets,
            meta=base_meta,
            failure_category=args.failure_category,
            error_excerpt=args.error_message,
        )
        _safe_mkdir(out_dir)
        (out_dir / "log.txt").write_text(
            f"[runner] failed stage={stage} failure_category={args.failure_category} timestamp_utc={_utc_timestamp()}\n"
            + (args.error_message + "\n" if args.error_message else ""),
            encoding="utf-8",
        )
        _write_json(results_path, payload)
        return 1

    # run
    cmd_tokens = list(args.command)
    if cmd_tokens and cmd_tokens[0] == "--":
        cmd_tokens = cmd_tokens[1:]

    require_python = any(tok == "{python}" for tok in cmd_tokens)
    report_path = resolve_report_path(args.report_path)

    resolved: Optional[PythonResolution] = None
    if require_python:
        resolved, res_fail = resolve_python(
            cli_python=args.cli_python,
            report_path=report_path,
        )
        if resolved is None:
            _safe_mkdir(out_dir)
            log_path.write_text(
                f"[runner] failed to resolve python (report_path={report_path})\n",
                encoding="utf-8",
            )
            payload = build_base_results(
                stage=stage,
                task=args.task,
                command_str="",
                timeout_sec=int(args.timeout_sec),
                framework=args.framework,
                exit_code=1,
                status="failure",
                skip_reason="not_applicable",
                assets=assets,
                meta={
                    **base_meta,
                    "python": "",
                    "report_path": str(report_path),
                },
                failure_category=res_fail or "missing_report",
                error_excerpt=_read_text_tail(log_path),
            )
            _write_json(results_path, payload)
            return 1

        cmd_tokens = [resolved.python if tok == "{python}" else tok for tok in cmd_tokens]
        base_meta["python"] = resolved.python
        base_meta["python_resolution"] = {
            "source": resolved.source,
            "warnings": resolved.warnings,
            "report_path": resolved.report_path,
            "report_python_path": resolved.report_python_path,
        }
    else:
        base_meta["python"] = sys.executable

    if not cmd_tokens:
        _safe_mkdir(out_dir)
        log_path.write_text("[runner] no command provided\n", encoding="utf-8")
        payload = build_base_results(
            stage=stage,
            task=args.task,
            command_str="",
            timeout_sec=int(args.timeout_sec),
            framework=args.framework,
            exit_code=1,
            status="failure",
            skip_reason="not_applicable",
            assets=assets,
            meta=base_meta,
            failure_category="args_unknown",
            error_excerpt=_read_text_tail(log_path),
        )
        _write_json(results_path, payload)
        return 1

    extra_env = _parse_env_kv(args.env)
    env = dict(os.environ)
    env.update(extra_env)
    base_meta["env_vars"] = extra_env

    cmd_str = shlex.join(cmd_tokens)
    exit_code, timed_out = run_command_capture(
        cmd=cmd_tokens,
        cwd=REPO_ROOT,
        env=env,
        timeout_sec=int(args.timeout_sec),
        log_path=log_path,
    )

    status = "success" if exit_code == 0 else "failure"
    failure_category = "not_applicable"
    if status == "failure":
        failure_category = "timeout" if timed_out else "runtime"

    payload = build_base_results(
        stage=stage,
        task=args.task,
        command_str=cmd_str,
        timeout_sec=int(args.timeout_sec),
        framework=args.framework,
        exit_code=exit_code,
        status=status,
        skip_reason="not_applicable",
        assets=assets,
        meta=base_meta,
        failure_category=failure_category,
        error_excerpt=_read_text_tail(log_path),
    )
    _write_json(results_path, payload)
    return 0 if status in ("success", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
