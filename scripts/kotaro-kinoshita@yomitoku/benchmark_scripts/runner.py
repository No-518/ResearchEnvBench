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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _safe_json_load(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(_read_text(path)), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json in {path}: {e}"
    except Exception as e:
        return None, f"failed to read {path}: {e}"


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


@dataclass(frozen=True)
class ResolvedPython:
    python: str
    source: str  # cli | env | report | fallback
    warning: Optional[str] = None
    report_path: Optional[str] = None
    reported_python_path: Optional[str] = None


def resolve_python(
    cli_python: Optional[str],
    report_path: Path,
    *,
    allow_fallback: bool = True,
) -> Tuple[Optional[ResolvedPython], Optional[str]]:
    if cli_python:
        return (
            ResolvedPython(
                python=cli_python,
                source="cli",
                report_path=str(report_path),
            ),
            None,
        )

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return (
            ResolvedPython(
                python=env_python,
                source="env",
                report_path=str(report_path),
            ),
            None,
        )

    report_obj, report_err = _safe_json_load(report_path)
    if report_obj is None:
        return None, f"missing_report: {report_err}"

    reported_python = report_obj.get("python_path")
    if isinstance(reported_python, str) and reported_python.strip():
        return (
            ResolvedPython(
                python=reported_python,
                source="report",
                report_path=str(report_path),
                reported_python_path=reported_python,
            ),
            None,
        )

    if allow_fallback:
        fallback = shutil.which("python") or "python"
        return (
            ResolvedPython(
                python=fallback,
                source="fallback",
                warning=f'report.json missing "python_path"; using fallback python from PATH: {fallback}',
                report_path=str(report_path),
                reported_python_path=None,
            ),
            None,
        )

    return None, 'missing_report: report.json missing "python_path" and fallback disabled'


def _git_commit(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
        return out or None
    except Exception:
        return None


def _tail_lines(path: Path, *, max_lines: int = 220) -> str:
    try:
        text = _read_text(path)
    except Exception:
        return ""
    lines = text.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)


def _default_assets() -> dict:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _sanitize_env_vars(keys: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            out[k] = v
    return out


def _quote_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _merge_assets_from_file(assets_from: Optional[str]) -> Tuple[dict, Optional[str]]:
    if not assets_from:
        return _default_assets(), None

    p = Path(assets_from)
    obj, err = _safe_json_load(p)
    if obj is None:
        return _default_assets(), err

    if isinstance(obj, dict) and "assets" in obj and isinstance(obj["assets"], dict):
        assets = obj["assets"]
    else:
        assets = obj

    if not isinstance(assets, dict):
        return _default_assets(), f"assets_from is not a JSON object: {assets_from}"

    merged = _default_assets()
    for k in ["dataset", "model"]:
        if isinstance(assets.get(k), dict):
            for kk in ["path", "source", "version", "sha256"]:
                v = assets[k].get(kk)
                if isinstance(v, str):
                    merged[k][kk] = v
    return merged, None


def _stage_results_base(
    *,
    stage: str,
    task: str,
    command_str: str,
    timeout_sec: int,
    framework: str,
    assets: dict,
    resolved_python: Optional[ResolvedPython],
    decision_reason: str,
    warnings: List[str],
    env_overrides: Dict[str, str],
) -> dict:
    repo_root = _repo_root()
    env_keys = [
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "HF_HUB_CACHE",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "PYTHONPATH",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
    ] + sorted(env_overrides.keys())

    meta = {
        "python": resolved_python.python if resolved_python else sys.executable,
        "python_resolution": {
            "source": resolved_python.source if resolved_python else "runner",
            "warning": resolved_python.warning if resolved_python else None,
            "report_path": resolved_python.report_path if resolved_python else None,
            "reported_python_path": resolved_python.reported_python_path if resolved_python else None,
        },
        "git_commit": _git_commit(repo_root),
        "timestamp_utc": _utc_now_iso(),
        "env_vars": _sanitize_env_vars(env_keys),
        "decision_reason": decision_reason,
        "warnings": warnings,
    }

    return {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": task,
        "command": command_str,
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": assets,
        "meta": meta,
        "failure_category": "unknown",
        "error_excerpt": "",
    }


def run_command(
    *,
    stage: str,
    task: str,
    out_dir: Path,
    timeout_sec: int,
    framework: str,
    cmd: List[str],
    env: Dict[str, str],
    assets: dict,
    resolved_python: Optional[ResolvedPython],
    decision_reason: str,
    warnings: List[str],
) -> Tuple[int, dict]:
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    command_str = _quote_cmd(cmd)

    results = _stage_results_base(
        stage=stage,
        task=task,
        command_str=command_str,
        timeout_sec=timeout_sec,
        framework=framework,
        assets=assets,
        resolved_python=resolved_python,
        decision_reason=decision_reason,
        warnings=warnings,
        env_overrides={k: env[k] for k in env.keys() if k not in os.environ or os.environ.get(k) != env[k]},
    )

    start = time.time()
    timed_out = False
    proc_rc: Optional[int] = None
    try:
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write(f"[runner] stage={stage} task={task} start_utc={_utc_now_iso()}\n")
            logf.write(f"[runner] cwd={_repo_root()}\n")
            logf.write(f"[runner] command={command_str}\n")
            logf.flush()

            proc = subprocess.Popen(
                cmd,
                cwd=str(_repo_root()),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                proc_rc = proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                proc_rc = proc.wait(timeout=30)
    except FileNotFoundError as e:
        results["failure_category"] = "entrypoint_not_found"
        results["error_excerpt"] = str(e)
        results["meta"]["timed_out"] = False
        results["meta"]["command_exit_code"] = None
        results["meta"]["duration_sec"] = round(time.time() - start, 3)
        _write_json(out_dir / "results.json", results)
        return 1, results
    except Exception as e:
        results["failure_category"] = "unknown"
        results["error_excerpt"] = str(e)
        results["meta"]["timed_out"] = timed_out
        results["meta"]["command_exit_code"] = proc_rc
        results["meta"]["duration_sec"] = round(time.time() - start, 3)
        _write_json(out_dir / "results.json", results)
        return 1, results

    results["meta"]["timed_out"] = timed_out
    results["meta"]["command_exit_code"] = proc_rc
    results["meta"]["duration_sec"] = round(time.time() - start, 3)

    if timed_out:
        results["status"] = "failure"
        results["exit_code"] = 1
        results["failure_category"] = "timeout"
        results["error_excerpt"] = _tail_lines(log_path)
        _write_json(out_dir / "results.json", results)
        return 1, results

    if proc_rc == 0:
        results["status"] = "success"
        results["exit_code"] = 0
        results["failure_category"] = "unknown"
        results["error_excerpt"] = ""
        _write_json(out_dir / "results.json", results)
        return 0, results

    results["status"] = "failure"
    results["exit_code"] = 1
    results["failure_category"] = "runtime"
    results["error_excerpt"] = _tail_lines(log_path)
    _write_json(out_dir / "results.json", results)
    return 1, results


def write_skipped(
    *,
    stage: str,
    task: str,
    out_dir: Path,
    timeout_sec: int,
    framework: str,
    command_str: str,
    skip_reason: str,
    assets: dict,
    resolved_python: Optional[ResolvedPython],
    decision_reason: str,
    warnings: List[str],
) -> Tuple[int, dict]:
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    with log_path.open("a", encoding="utf-8") as logf:
        logf.write(f"[runner] stage={stage} task={task} skipped\n")
        logf.write(f"[runner] reason={skip_reason}\n")
        logf.write(f"[runner] decision={decision_reason}\n")

    results = _stage_results_base(
        stage=stage,
        task=task,
        command_str=command_str,
        timeout_sec=timeout_sec,
        framework=framework,
        assets=assets,
        resolved_python=resolved_python,
        decision_reason=decision_reason,
        warnings=warnings,
        env_overrides={},
    )
    results["status"] = "skipped"
    results["skip_reason"] = skip_reason
    results["exit_code"] = 0
    results["failure_category"] = "unknown"
    results["error_excerpt"] = ""
    results["meta"]["timed_out"] = False
    results["meta"]["command_exit_code"] = None
    results["meta"]["duration_sec"] = 0.0
    _write_json(out_dir / "results.json", results)
    return 0, results


def _parse_env_overrides(values: List[str]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"--env must be KEY=VALUE, got: {item}")
        k, v = item.split("=", 1)
        env[k] = v
    return env


def _cmd_from_args(cmd_args: List[str]) -> List[str]:
    if not cmd_args:
        raise ValueError("command is required after --")
    return cmd_args


def cmd_resolve_python(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="runner.py resolve-python")
    ap.add_argument("--python", default=None)
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args(argv)

    report_path = resolve_report_path(args.report_path)
    resolved, err = resolve_python(args.python, report_path)
    if resolved is None:
        sys.stderr.write((err or "missing_report") + "\n")
        return 1
    sys.stdout.write(resolved.python + "\n")
    return 0


def cmd_run(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="runner.py run")
    ap.add_argument("--stage", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--timeout-sec", type=int, required=True)
    ap.add_argument("--framework", default="unknown")
    ap.add_argument("--python", default=None)
    ap.add_argument("--report-path", default=None)
    ap.add_argument("--assets-from", default=None, help="Path to JSON containing {assets:{dataset,model}} or just {dataset,model}")
    ap.add_argument("--decision-reason", default="")
    ap.add_argument("--skip-reason", default=None, help="If set, do not run command and write skipped results.")
    ap.add_argument("--requires-python", action="store_true", default=False)
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE overrides for subprocess env")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)

    if args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    cmd = _cmd_from_args(args.cmd)

    out_dir = _repo_root() / args.out_dir
    assets, assets_err = _merge_assets_from_file(args.assets_from)
    warnings: List[str] = []
    if assets_err:
        warnings.append(f"assets_from_error: {assets_err}")

    report_path = resolve_report_path(args.report_path)
    resolved_python: Optional[ResolvedPython] = None
    needs_py = args.requires_python or "{python}" in cmd
    if needs_py:
        resolved_python, err = resolve_python(args.python, report_path)
        if resolved_python is None:
            _ensure_dir(out_dir)
            (out_dir / "log.txt").write_text((err or "missing_report") + "\n", encoding="utf-8")

            results = _stage_results_base(
                stage=args.stage,
                task=args.task,
                command_str=_quote_cmd(cmd),
                timeout_sec=args.timeout_sec,
                framework=args.framework,
                assets=assets,
                resolved_python=None,
                decision_reason=args.decision_reason,
                warnings=warnings + [err or "missing_report"],
                env_overrides={},
            )
            results["failure_category"] = "missing_report"
            results["error_excerpt"] = (err or "missing_report")
            _write_json(out_dir / "results.json", results)
            return 1

        if resolved_python.warning:
            warnings.append(resolved_python.warning)

    cmd = [resolved_python.python if x == "{python}" and resolved_python else x for x in cmd]
    command_str = _quote_cmd(cmd)

    if args.skip_reason:
        rc, _ = write_skipped(
            stage=args.stage,
            task=args.task,
            out_dir=out_dir,
            timeout_sec=args.timeout_sec,
            framework=args.framework,
            command_str=command_str,
            skip_reason=args.skip_reason,
            assets=assets,
            resolved_python=resolved_python,
            decision_reason=args.decision_reason,
            warnings=warnings,
        )
        return rc

    env = dict(os.environ)
    env_overrides = _parse_env_overrides(args.env)
    env.update(env_overrides)

    rc, _ = run_command(
        stage=args.stage,
        task=args.task,
        out_dir=out_dir,
        timeout_sec=args.timeout_sec,
        framework=args.framework,
        cmd=cmd,
        env=env,
        assets=assets,
        resolved_python=resolved_python,
        decision_reason=args.decision_reason,
        warnings=warnings,
    )
    return rc


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        sys.stdout.write(
            "runner.py subcommands:\n"
            "  resolve-python [--python PATH] [--report-path PATH]\n"
            "  run --stage S --task T --out-dir DIR --timeout-sec N [--framework F] [--requires-python]\n"
            "      [--python PATH] [--report-path PATH] [--assets-from JSON_PATH]\n"
            "      [--env KEY=VALUE ...] [--decision-reason TEXT] [--skip-reason REASON]\n"
            "      -- <command...> (use {python} placeholder to inject resolved python)\n"
        )
        return 0

    sub = sys.argv[1]
    argv = sys.argv[2:]
    if sub == "resolve-python":
        return cmd_resolve_python(argv)
    if sub == "run":
        return cmd_run(argv)

    sys.stderr.write(f"unknown subcommand: {sub}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
