#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STAGE_TIMEOUT_SEC: dict[str, int] = {
    "prepare": 1200,
    "cpu": 600,
    "cuda": 120,
    "single_gpu": 600,
    "multi_gpu": 1200,
    "env_size": 120,
    "hallucination": 120,
    "summary": 120,
    "pyright": 600,
}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        text = _read_text(path)
    except FileNotFoundError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _format_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def _git_commit(repo: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


def _load_json_file(path: Path) -> Any:
    return json.loads(_read_text(path))


def _default_assets() -> dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def _resolve_python(
    cli_python: str | None,
    report_path: Path,
    *,
    require_report: bool,
) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {
        "report_path": str(report_path),
        "source": "",
        "warning": "",
    }

    if cli_python:
        meta["source"] = "cli"
        return cli_python, meta

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        meta["source"] = "env:SCIMLOPSBENCH_PYTHON"
        return env_python, meta

    report: dict[str, Any] | None = None
    try:
        report = _load_json_file(report_path)
    except Exception as e:
        if require_report:
            raise RuntimeError(f"missing/invalid report.json at {report_path}: {e}") from e
        report = None

    if report and isinstance(report.get("python_path"), str) and report["python_path"].strip():
        candidate = report["python_path"]
        if Path(candidate).exists() and os.access(candidate, os.X_OK):
            meta["source"] = "report:python_path"
            return candidate, meta
        # Report exists but python_path is unusable; proceed with PATH python as a last resort.
        py = shutil.which("python3") or shutil.which("python") or "python"
        meta["source"] = "fallback:PATH"
        meta["reported_python_path"] = candidate
        meta["warning"] = f"Reported python_path is not executable ({candidate}); falling back to {py}."
        return py, meta

    py = shutil.which("python3") or shutil.which("python") or "python"
    meta["source"] = "fallback:PATH"
    meta["warning"] = "Report missing/invalid; falling back to python from PATH."
    return py, meta


def _parse_env_kv(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--env must be KEY=VALUE, got: {item!r}")
        k, v = item.split("=", 1)
        if not k:
            raise ValueError(f"--env must be KEY=VALUE, got: {item!r}")
        out[k] = v
    return out


def _infer_failure_category(return_code: int | None, timed_out: bool, stderr_excerpt: str) -> str:
    if timed_out:
        return "timeout"
    if return_code is None:
        return "unknown"
    if return_code == 127:
        return "entrypoint_not_found"
    if "No module named" in stderr_excerpt:
        return "deps"
    return "runtime" if return_code != 0 else ""


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark stage runner (writes log.txt and results.json).")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure", "summary"])
    parser.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    parser.add_argument("--timeout-sec", type=int, default=0)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--python", dest="python_path", default="")
    parser.add_argument("--report-path", default="")
    parser.add_argument("--requires-python", action="store_true", default=True)
    parser.add_argument("--no-requires-python", dest="requires_python", action="store_false")
    parser.add_argument("--assets-json", default="")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--env", action="append", default=[], help="Environment override KEY=VALUE (repeatable)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    repo = _repo_root()
    stage = args.stage
    out_dir = Path(args.out_dir) if args.out_dir else repo / "build_output" / stage
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = args.timeout_sec or DEFAULT_STAGE_TIMEOUT_SEC.get(stage, 600)
    report_path = _resolve_report_path(args.report_path)

    result: dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": args.task,
        "command": "",
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": _default_assets(),
        "meta": {
            "python": "",
            "git_commit": _git_commit(repo),
            "env_vars": {},
            "decision_reason": args.decision_reason,
            "timestamp_utc": _utc_timestamp(),
            "python_resolution": {},
            "warnings": [],
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    try:
        assets_path = Path(args.assets_json) if args.assets_json else None
        if assets_path and assets_path.exists():
            try:
                loaded_assets = _load_json_file(assets_path)
                if isinstance(loaded_assets, dict):
                    result["assets"] = {
                        "dataset": dict(_default_assets()["dataset"], **(loaded_assets.get("dataset") or {})),
                        "model": dict(_default_assets()["model"], **(loaded_assets.get("model") or {})),
                    }
            except Exception:
                result["meta"]["warnings"].append(f"Failed to load assets json: {assets_path}")

        env_overrides = _parse_env_kv(args.env)
        child_env = os.environ.copy()
        child_env.update(env_overrides)
        result["meta"]["env_vars"] = env_overrides

        resolved_python = ""
        python_meta: dict[str, Any] = {}
        if args.requires_python:
            try:
                resolved_python, python_meta = _resolve_python(
                    args.python_path or None,
                    report_path,
                    require_report=True,
                )
            except Exception:
                result["failure_category"] = "missing_report"
                raise
            result["meta"]["python"] = resolved_python
            result["meta"]["python_resolution"] = python_meta

        cmd = [c for c in args.command if c != "--"]
        if not cmd:
            raise RuntimeError("No command provided after '--'.")

        if resolved_python and cmd[0] in {"python", "python3"}:
            cmd[0] = resolved_python

        result["command"] = _format_cmd(cmd)

        with log_path.open("w", encoding="utf-8") as log_fp:
            log_fp.write(f"[runner] stage={stage} task={args.task} timeout_sec={timeout_sec}\n")
            if resolved_python:
                log_fp.write(f"[runner] resolved_python={resolved_python} ({python_meta.get('source','')})\n")
                if python_meta.get("warning"):
                    log_fp.write(f"[runner] warning: {python_meta['warning']}\n")
            log_fp.write(f"[runner] cwd={repo}\n")
            log_fp.write(f"[runner] command={result['command']}\n")
            log_fp.flush()

            timed_out = False
            completed: subprocess.CompletedProcess[str] | None = None
            try:
                completed = subprocess.run(
                    cmd,
                    cwd=str(repo),
                    env=child_env,
                    stdout=log_fp,
                    stderr=log_fp,
                    text=True,
                    timeout=timeout_sec,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
            except FileNotFoundError:
                result["failure_category"] = "entrypoint_not_found"
                raise

        excerpt = _tail_lines(log_path, max_lines=240)
        result["error_excerpt"] = excerpt

        if timed_out:
            result["status"] = "failure"
            result["exit_code"] = 1
            result["failure_category"] = "timeout"
        else:
            assert completed is not None
            result["meta"]["process_exit_code"] = completed.returncode
            if completed.returncode == 0:
                result["status"] = "success"
                result["exit_code"] = 0
                result["failure_category"] = ""
                result["error_excerpt"] = ""
            else:
                result["status"] = "failure"
                result["exit_code"] = 1
                result["failure_category"] = _infer_failure_category(completed.returncode, False, excerpt)

    except Exception as e:
        result["status"] = "failure"
        result["exit_code"] = 1
        if result.get("failure_category") in {"", "unknown"}:
            result["failure_category"] = "unknown"
        result["error_excerpt"] = _tail_lines(log_path, max_lines=240)
        result["meta"]["exception"] = f"{type(e).__name__}: {e}"
        result["meta"]["traceback"] = traceback.format_exc(limit=50)
    finally:
        try:
            _write_json(results_path, result)
        except Exception:
            pass

    return 0 if result["status"] in {"success", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
