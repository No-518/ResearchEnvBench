#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent


def _shlex_join(tokens: Sequence[str]) -> str:
    try:
        return shlex.join(list(tokens))
    except AttributeError:
        return " ".join(shlex.quote(t) for t in tokens)


def _safe_read_text(path: Path, max_bytes: int = 2_000_000) -> str:
    try:
        with path.open("rb") as f:
            data = f.read(max_bytes + 1)
        if len(data) > max_bytes:
            data = data[:max_bytes]
        return data.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def tail_lines(path: Path, max_lines: int = 220) -> str:
    text = _safe_read_text(path)
    if not text:
        return ""
    lines = text.splitlines()
    tail = lines[-max_lines:]
    return "\n".join(tail).strip()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(DEFAULT_REPORT_PATH)


def get_git_commit(root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


@dataclass(frozen=True)
class PythonResolution:
    python_cmd: List[str]
    source: str
    warnings: List[str]


def _parse_python_cmd(value: str) -> List[str]:
    tokens = shlex.split(value)
    return tokens if tokens else [value]


def resolve_python_cmd(
    cli_python: Optional[str],
    report_path: Path,
    *,
    requires_python: bool,
) -> PythonResolution:
    warnings: List[str] = []

    if cli_python:
        return PythonResolution(_parse_python_cmd(cli_python), "cli", warnings)

    if os.environ.get("SCIMLOPSBENCH_PYTHON"):
        return PythonResolution(_parse_python_cmd(os.environ["SCIMLOPSBENCH_PYTHON"]), "env", warnings)

    try:
        report = read_json(report_path)
    except Exception as e:
        if requires_python:
            raise RuntimeError(f"missing_report: failed to read report at {report_path}: {e}") from e
        python_fallback = shutil.which("python") or shutil.which("python3") or sys.executable
        warnings.append("Report missing/invalid; using python from PATH as fallback.")
        return PythonResolution([python_fallback], "path_fallback", warnings)

    python_path = report.get("python_path")
    if not python_path:
        if requires_python:
            raise RuntimeError(f"missing_report: report has no 'python_path' field: {report_path}")
        python_fallback = shutil.which("python") or shutil.which("python3") or sys.executable
        warnings.append("Report missing python_path; using python from PATH as fallback.")
        return PythonResolution([python_fallback], "path_fallback", warnings)

    return PythonResolution([str(python_path)], "report", warnings)


def _python_version(python_cmd: Sequence[str], timeout_sec: int = 20) -> str:
    try:
        out = subprocess.check_output(
            list(python_cmd)
            + [
                "-c",
                "import platform; print(platform.python_version())",
            ],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
        ).strip()
        return out
    except Exception:
        return ""


def _deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _collect_env_vars(keys: Iterable[str]) -> Dict[str, str]:
    env_out: Dict[str, str] = {}
    for k in keys:
        if k in os.environ:
            v = os.environ.get(k, "")
            if any(s in k.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "KEY")) and v:
                v = "***redacted***"
            env_out[k] = v
    return env_out


def _run_with_timeout(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    log_path: Path,
    timeout_sec: int,
) -> Tuple[int, bool]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_f:
        log_f.write(f"[runner] utc_start={_utc_now_iso()}\n")
        log_f.write(f"[runner] cwd={cwd}\n")
        log_f.write(f"[runner] cmd={_shlex_join(cmd)}\n")
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
        try:
            rc = proc.wait(timeout=timeout_sec)
            log_f.write(f"[runner] return_code={rc}\n")
            return rc, False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            log_f.write(f"[runner] timeout_sec={timeout_sec}\n")
            return 124, True


def _default_assets() -> Dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def build_base_results(
    *,
    stage: str,
    task: str,
    command: str,
    timeout_sec: int,
    framework: str,
    status: str,
    skip_reason: str,
    failure_category: str,
    error_excerpt: str,
    python_cmd: Optional[Sequence[str]],
    python_source: str,
    python_warnings: Sequence[str],
    decision_reason: str,
    assets: Dict[str, Any],
    env_overrides: Optional[Dict[str, str]] = None,
    meta_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    root = repo_root()
    env_interest = [
        "CUDA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "SCIMLOPSBENCH_DATASET_URL",
        "SCIMLOPSBENCH_MODEL_URL",
        "SCIMLOPSBENCH_MULTI_GPU_DEVICES",
    ]
    meta: Dict[str, Any] = {
        "python": _shlex_join(list(python_cmd)) if python_cmd else "",
        "git_commit": get_git_commit(root),
        "env_vars": _collect_env_vars(env_interest),
        "decision_reason": decision_reason,
        "python_resolution": {
            "source": python_source,
            "warnings": list(python_warnings),
        },
    }
    if env_overrides:
        for k, v in env_overrides.items():
            vv = v
            if any(s in k.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "KEY")) and vv:
                vv = "***redacted***"
            meta["env_vars"][k] = vv
    if python_cmd:
        meta["python_resolution"]["version"] = _python_version(python_cmd)
    if meta_extra:
        _deep_merge(meta, meta_extra)

    exit_code = 0 if status in ("success", "skipped") else 1
    return {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": exit_code,
        "stage": stage,
        "task": task,
        "command": command,
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": assets,
        "meta": meta,
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }


def _parse_env_kv(pairs: Sequence[str]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise ValueError(f"--env must be KEY=VALUE, got: {item}")
        k, v = item.split("=", 1)
        env[k] = v
    return env


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark command runner.")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--framework", default="unknown")
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python", dest="cli_python", default=None)
    parser.add_argument("--requires-python", action="store_true")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--skip", action="store_true")
    parser.add_argument("--skip-reason", default="unknown")
    parser.add_argument("--failure-category", default="unknown")
    parser.add_argument("--assets-json", default=None)
    parser.add_argument("--meta-json", default=None)
    parser.add_argument("--results-extra-json", default=None)
    parser.add_argument("--allow-nonzero-exit", action="store_true")
    parser.add_argument("--env", action="append", default=[], help="Extra env var KEY=VALUE (repeatable).")

    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument("--python-code", default=None)
    run_mode.add_argument("--python-script", default=None)
    run_mode.add_argument("--python-module", default=None)

    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run after --")
    args = parser.parse_args(argv)

    root = repo_root()
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = (root / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    env_overrides = _parse_env_kv(args.env)

    python_cmd: Optional[List[str]] = None
    python_source = ""
    python_warnings: List[str] = []

    requires_python = bool(args.requires_python or args.python_code or args.python_script or args.python_module)

    if args.timeout_sec is None:
        default_timeouts = {
            "prepare": 1200,
            "pyright": 600,
            "cpu": 600,
            "cuda": 120,
            "single_gpu": 600,
            "multi_gpu": 1200,
            "env_size": 120,
            "hallucination": 120,
            "summary": 120,
        }
        args.timeout_sec = int(default_timeouts.get(str(args.stage), 600))

    def _load_optional_json(path_str: Optional[str]) -> Dict[str, Any]:
        if not path_str:
            return {}
        p = Path(path_str)
        if not p.is_absolute():
            p = (root / p).resolve()
        try:
            return read_json(p)
        except Exception:
            return {}

    assets: Dict[str, Any] = _default_assets()
    if args.assets_json:
        assets = _load_optional_json(args.assets_json) or _default_assets()
        if "dataset" not in assets or "model" not in assets:
            assets = _default_assets()

    meta_extra = _load_optional_json(args.meta_json) if args.meta_json else None

    command_tokens: List[str] = []
    command_str = ""
    status = "failure"
    skip_reason = args.skip_reason if args.skip else "unknown"
    failure_category = args.failure_category
    timed_out = False
    rc: Optional[int] = None

    try:
        if args.skip:
            command_str = "<skipped>"
            status = "skipped"
            failure_category = "unknown"
        else:
            if requires_python:
                res = resolve_python_cmd(args.cli_python, report_path, requires_python=True)
                python_cmd = res.python_cmd
                python_source = res.source
                python_warnings = res.warnings
            else:
                res = resolve_python_cmd(args.cli_python, report_path, requires_python=False)
                python_cmd = res.python_cmd
                python_source = res.source
                python_warnings = res.warnings

            if args.python_code:
                command_tokens = list(python_cmd) + ["-c", args.python_code]
            elif args.python_script:
                command_tokens = list(python_cmd) + [args.python_script] + (args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd)
            elif args.python_module:
                command_tokens = list(python_cmd) + ["-m", args.python_module] + (args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd)
            else:
                cmd = args.cmd
                if cmd[:1] == ["--"]:
                    cmd = cmd[1:]
                if not cmd:
                    raise RuntimeError("No command provided (use -- <cmd...>).")
                command_tokens = list(cmd)

            command_str = _shlex_join(command_tokens)

            env = os.environ.copy()
            env.update(env_overrides)
            env.setdefault("PYTHONUNBUFFERED", "1")

            rc, timed_out = _run_with_timeout(
                command_tokens,
                cwd=root,
                env=env,
                log_path=log_path,
                timeout_sec=args.timeout_sec,
            )

            if timed_out:
                status = "failure"
                failure_category = "timeout"
            elif rc == 0 or args.allow_nonzero_exit:
                status = "success"
                failure_category = "unknown"
            else:
                status = "failure"
                if failure_category == "unknown":
                    failure_category = "runtime"

        error_excerpt = tail_lines(log_path)

        results = build_base_results(
            stage=args.stage,
            task=args.task,
            command=command_str,
            timeout_sec=args.timeout_sec,
            framework=args.framework,
            status=status,
            skip_reason=skip_reason,
            failure_category=failure_category,
            error_excerpt=error_excerpt,
            python_cmd=python_cmd,
            python_source=python_source,
            python_warnings=python_warnings,
            decision_reason=args.decision_reason,
            assets=assets,
            env_overrides=env_overrides,
            meta_extra=meta_extra,
        )
        if rc is not None:
            results["meta"]["return_code"] = rc
            results["meta"]["timed_out"] = timed_out

        if args.results_extra_json:
            extra = _load_optional_json(args.results_extra_json)
            if isinstance(extra, dict) and extra:
                _deep_merge(results, extra)
                results["error_excerpt"] = tail_lines(log_path)

        write_json(results_path, results)
        return int(results.get("exit_code", 1))
    except Exception as e:
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write(f"[runner] exception: {e}\n")
            log_f.write(traceback.format_exc())
            log_f.write("\n")

        error_excerpt = tail_lines(log_path)
        results = build_base_results(
            stage=args.stage,
            task=args.task,
            command=command_str or "<runner_exception>",
            timeout_sec=args.timeout_sec,
            framework=args.framework,
            status="failure",
            skip_reason="unknown",
            failure_category="missing_report" if str(e).startswith("missing_report:") else "unknown",
            error_excerpt=error_excerpt,
            python_cmd=python_cmd,
            python_source=python_source,
            python_warnings=python_warnings,
            decision_reason=args.decision_reason,
            assets=assets,
            env_overrides=env_overrides,
            meta_extra=meta_extra,
        )
        write_json(results_path, results)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
