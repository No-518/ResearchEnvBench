#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text_tail(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            data = f.read()
        lines = data.splitlines()[-max_lines:]
        return b"\n".join(lines).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).strip()
        return out
    except Exception:
        return ""


def _parse_key_value(kv: str) -> Tuple[str, str]:
    if "=" not in kv:
        raise ValueError(f"Expected KEY=VALUE, got: {kv}")
    k, v = kv.split("=", 1)
    return k, v


def _load_report(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not report_path.exists():
        return None, f"report not found at {report_path}"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "report JSON is not an object"
        return data, None
    except Exception as e:
        return None, f"failed to read/parse report: {e}"


def _resolve_python(
    cli_python: Optional[str], report_path: Path
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    meta: Dict[str, Any] = {"python_resolution": {"used_fallback_path_python": False}}

    if cli_python:
        meta["python_resolution"].update({"source": "cli", "python": cli_python})
        return cli_python, meta, None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        meta["python_resolution"].update({"source": "env:SCIMLOPSBENCH_PYTHON", "python": env_python})
        return env_python, meta, None

    report, report_err = _load_report(report_path)
    if report is None:
        meta["python_resolution"].update({"source": "report", "error": report_err})
        return None, meta, "missing_report"

    python_path = report.get("python_path")
    if not python_path or not isinstance(python_path, str):
        meta["python_resolution"].update({"source": "report", "error": "python_path missing/invalid"})
        return None, meta, "missing_report"

    meta["python_resolution"].update({"source": "report", "python": python_path})
    return python_path, meta, None


def _is_executable_file(path: str) -> bool:
    try:
        p = Path(path)
        return p.exists() and os.access(str(p), os.X_OK) and p.is_file()
    except Exception:
        return False


def _command_str(argv: List[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark stage runner.")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--timeout-sec", type=int, default=600)
    parser.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    parser.add_argument("--python", dest="cli_python", default="")
    parser.add_argument("--report-path", default="")
    parser.add_argument("--assets-json", default="", help="Optional JSON file with assets to embed into results.json")
    parser.add_argument("--extra-json", default="", help="Optional JSON file to deep-merge into results.json")
    parser.add_argument("--failure-category", default="", help="Default failure_category on non-zero exit")
    parser.add_argument("--env", action="append", default=[], help="Additional env var KEY=VALUE (repeatable)")
    parser.add_argument("--skip", default="", help="If set, write skipped results with this skip_reason")
    parser.add_argument("--skip-message", default="", help="Optional human-readable skip explanation")
    parser.add_argument("--skip-command", default="", help="Optional command string to record when skipped")
    parser.add_argument("--", dest="cmd_sep", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    repo_root = _repo_root()
    stage = args.stage
    out_dir = Path(args.out_dir) if args.out_dir else repo_root / "build_output" / stage
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    _safe_mkdir(out_dir)

    # Ensure we always write results.json, even on internal failures.
    base_results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": args.task,
        "command": "",
        "timeout_sec": int(args.timeout_sec),
        "framework": args.framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_now_iso(),
            "env_vars": {},
            "decision_reason": "",
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    # Merge assets if provided.
    if args.assets_json:
        try:
            assets_obj = json.loads(Path(args.assets_json).read_text(encoding="utf-8"))
            if isinstance(assets_obj, dict):
                base_results["assets"] = assets_obj
        except Exception:
            # Leave defaults; caller can inspect log.
            pass

    # Prepare env.
    env = os.environ.copy()
    env_overrides: Dict[str, str] = {}
    for kv in args.env:
        k, v = _parse_key_value(kv)
        env[k] = v
        env_overrides[k] = v
    # Record a small, relevant subset of env vars + overrides.
    interesting = [
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "WANDB_MODE",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
    ]
    env_snapshot = {k: env.get(k, "") for k in interesting if k in env or k in env_overrides}
    env_snapshot.update(env_overrides)
    base_results["meta"]["env_vars"] = env_snapshot

    # Skip path.
    if args.skip:
        base_results.update(
            {
                "status": "skipped",
                "skip_reason": args.skip,
                "exit_code": 0,
                "command": args.skip_command or "",
                "failure_category": "not_applicable",
            }
        )
        if args.skip_message:
            base_results["meta"]["decision_reason"] = args.skip_message
        _write_json(results_path, base_results)
        log_path.write_text((args.skip_message or "skipped") + "\n", encoding="utf-8")
        return 0

    cmd = [c for c in args.cmd if c != "--"]
    if not cmd:
        base_results["failure_category"] = "args_unknown"
        base_results["error_excerpt"] = "No command provided to runner."
        _write_json(results_path, base_results)
        log_path.write_text("No command provided to runner.\n", encoding="utf-8")
        return 1

    # Python placeholder substitution.
    report_path = Path(
        args.report_path
        or os.environ.get("SCIMLOPSBENCH_REPORT")
        or "/opt/scimlopsbench/report.json"
    )
    needs_python = any(tok == "{python}" for tok in cmd)
    python_path: Optional[str] = None
    python_meta: Dict[str, Any] = {}
    python_err_category: Optional[str] = None
    if needs_python:
        python_path, python_meta, python_err_category = _resolve_python(
            args.cli_python or None, report_path
        )
        base_results["meta"].update(python_meta)
        if not python_path:
            base_results["failure_category"] = python_err_category or "missing_report"
            base_results["error_excerpt"] = python_meta.get("python_resolution", {}).get("error", "")
            _write_json(results_path, base_results)
            log_path.write_text(base_results["error_excerpt"] + "\n", encoding="utf-8")
            return 1
        if not _is_executable_file(python_path):
            base_results["failure_category"] = "path_hallucination"
            base_results["error_excerpt"] = f"Resolved python_path is not executable: {python_path}"
            _write_json(results_path, base_results)
            log_path.write_text(base_results["error_excerpt"] + "\n", encoding="utf-8")
            return 1
        cmd = [python_path if tok == "{python}" else tok for tok in cmd]

    base_results["command"] = _command_str(cmd)

    # Execute.
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log_f:
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(repo_root),
                    env=env,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=args.timeout_sec,
                    check=False,
                )
                proc_rc = int(proc.returncode)
            except FileNotFoundError as e:
                base_results["failure_category"] = "entrypoint_not_found"
                base_results["error_excerpt"] = str(e)
                _write_json(results_path, base_results)
                return 1
            except subprocess.TimeoutExpired:
                base_results["failure_category"] = "timeout"
                base_results["error_excerpt"] = _read_text_tail(log_path)
                _write_json(results_path, base_results)
                return 1
    except Exception as e:
        base_results["failure_category"] = "unknown"
        base_results["error_excerpt"] = f"runner internal error: {e}"
        _write_json(results_path, base_results)
        try:
            log_path.write_text(base_results["error_excerpt"] + "\n", encoding="utf-8")
        except Exception:
            pass
        return 1

    base_results["meta"]["process_returncode"] = proc_rc
    if proc_rc == 0:
        base_results["status"] = "success"
        base_results["skip_reason"] = "unknown"
        base_results["exit_code"] = 0
        base_results["failure_category"] = "unknown"
        base_results["error_excerpt"] = ""
        # Allow command output JSON to add details via --extra-json.
    else:
        base_results["status"] = "failure"
        base_results["exit_code"] = 1
        base_results["failure_category"] = args.failure_category or "runtime"
        base_results["error_excerpt"] = _read_text_tail(log_path)

    # Deep-merge extra JSON.
    if args.extra_json:
        try:
            extra_obj = json.loads(Path(args.extra_json).read_text(encoding="utf-8"))
            if isinstance(extra_obj, dict):
                # Shallow merge for top-level keys; nested dicts merged one-level deep.
                for k, v in extra_obj.items():
                    if isinstance(v, dict) and isinstance(base_results.get(k), dict):
                        base_results[k].update(v)  # type: ignore[index]
                    else:
                        base_results[k] = v
        except Exception:
            pass

    _write_json(results_path, base_results)
    return 0 if base_results["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
