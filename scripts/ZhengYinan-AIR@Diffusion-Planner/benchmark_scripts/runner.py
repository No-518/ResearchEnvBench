#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _stage_dir(stage: str) -> Path:
    return _repo_root() / "build_output" / stage


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _tail_text(path: Path, max_lines: int = 220, max_chars: int = 20000) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = "".join(lines[-max_lines:])
        if len(tail) > max_chars:
            tail = tail[-max_chars:]
        return tail.strip()
    except Exception as e:
        return f"[runner] failed to read log for excerpt: {e}"


def _git_commit(repo: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


def _select_env_vars() -> Dict[str, str]:
    allow_prefixes = (
        "SCIMLOPSBENCH_",
        "CUDA",
        "NCCL_",
        "HF_",
        "TRANSFORMERS_",
        "TORCH_",
    )
    allow_exact = {"PATH", "PYTHONPATH"}
    env: Dict[str, str] = {}
    for k, v in os.environ.items():
        if k in allow_exact or any(k.startswith(p) for p in allow_prefixes):
            env[k] = v
    return env


def _resolve_report_path(cli_report_path: Optional[str]) -> str:
    if cli_report_path:
        return cli_report_path
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return os.environ["SCIMLOPSBENCH_REPORT"]
    return DEFAULT_REPORT_PATH


@dataclass
class PythonResolution:
    python_path: str
    source: str  # cli|env|report|path_fallback
    warning: str = ""


def _read_report_python_path(report_path: str) -> Tuple[Optional[str], Optional[str]]:
    p = Path(report_path)
    if not p.exists():
        return None, f"report not found: {report_path}"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"report invalid json: {e}"
    py = data.get("python_path")
    if not isinstance(py, str) or not py.strip():
        return None, "report missing python_path"
    return py, None


def resolve_python(
    cli_python: Optional[str],
    report_path: str,
) -> Tuple[Optional[PythonResolution], Optional[str]]:
    if cli_python:
        return PythonResolution(cli_python, "cli"), None
    if os.environ.get("SCIMLOPSBENCH_PYTHON"):
        return PythonResolution(os.environ["SCIMLOPSBENCH_PYTHON"], "env"), None

    py, err = _read_report_python_path(report_path)
    if not py:
        return None, err or "unable to resolve python from report"

    # Prefer the report python when it is runnable; otherwise fall back to PATH python (record warning).
    if Path(py).exists() and os.access(py, os.X_OK):
        return PythonResolution(py, "report"), None

    fallback = shutil_which_python()
    if fallback:
        return (
            PythonResolution(
                fallback,
                "path_fallback",
                warning=f"Report python_path is not executable ({py}); falling back to python from PATH.",
            ),
            None,
        )

    return None, f"report python_path is not executable ({py}) and no python found on PATH"


def shutil_which_python() -> Optional[str]:
    for exe in ("python", "python3"):
        try:
            out = subprocess.check_output(["bash", "-lc", f"command -v {shlex.quote(exe)}"], text=True).strip()
            if out:
                return out
        except Exception:
            continue
    return None


def _default_timeout_for_stage(stage: str) -> int:
    defaults = {
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
    return int(defaults.get(stage, 600))


def _load_prepare_assets(repo: Path) -> Dict[str, Any]:
    p = repo / "build_output" / "prepare" / "results.json"
    if not p.exists():
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        assets = data.get("assets", {})
        dataset = assets.get("dataset", {}) if isinstance(assets, dict) else {}
        model = assets.get("model", {}) if isinstance(assets, dict) else {}
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
    except Exception:
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }


def _stringify_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def run_subprocess(
    cmd: List[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    timeout_sec: int,
    log_path: Path,
) -> Tuple[int, bool, Optional[str]]:
    try:
        with log_path.open("a", encoding="utf-8") as logf:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                proc.wait(timeout=timeout_sec)
                return proc.returncode, False, None
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=30)
                except Exception:
                    pass
                return 124, True, f"timeout after {timeout_sec}s"
    except FileNotFoundError as e:
        return 127, False, f"entrypoint not found: {e}"
    except Exception as e:
        return 1, False, f"runner exception: {e}"


def _infer_failure_category(error_excerpt: str, timed_out: bool, cmd_rc: int) -> str:
    if timed_out:
        return "timeout"
    if cmd_rc == 127:
        return "entrypoint_not_found"
    lower = error_excerpt.lower()
    if "unrecognized arguments" in lower or "unknown argument" in lower:
        return "args_unknown"
    if "no module named" in lower or "moduleNotFoundError".lower() in lower:
        return "deps"
    if "permission denied" in lower:
        return "runtime"
    if "out of memory" in lower or "cuda out of memory" in lower:
        return "oom"
    return "runtime"


def _ensure_python_modules(
    python_exe: str,
    ensure_specs: List[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    log_path: Path,
) -> Tuple[bool, List[Dict[str, Any]], Optional[str], Optional[str], List[str]]:
    """
    Ensure modules are importable (check-only; no installation).

    Returns:
      ok, details, failure_category, error_message, executed_command_pieces
    """
    details: List[Dict[str, Any]] = []
    executed: List[str] = []

    for spec in ensure_specs:
        if not spec or not spec.strip():
            continue
        if "=" in spec:
            module, pkg = spec.split("=", 1)
        else:
            module, pkg = spec, spec
        module = module.strip()
        pkg = pkg.strip()
        if not module:
            continue

        entry: Dict[str, Any] = {
            "module": module,
            "package": pkg,
            "import_ok": False,
            "check_command": "",
            "check_returncode": None,
        }

        entry["check_command"] = f"{python_exe} -c 'import {module}'"
        executed.append(entry["check_command"])
        rc, timed_out, _ = run_subprocess(
            [python_exe, "-c", f"import {module}"],
            cwd=cwd,
            env=env,
            timeout_sec=60,
            log_path=log_path,
        )
        entry["check_returncode"] = rc
        entry["import_ok"] = bool(rc == 0 and not timed_out)
        details.append(entry)
        if not entry["import_ok"]:
            return (
                False,
                details,
                "deps",
                f"Missing required python module '{module}' (expected package '{pkg}').",
                executed,
            )

    return True, details, None, None, executed


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark command runner.")
    parser.add_argument("--stage", required=False, help="Stage name (e.g., cpu, single_gpu).")
    parser.add_argument("--task", default="run", help="Task label (train|infer|check|download|validate|measure).")
    parser.add_argument("--framework", default="unknown", help="Framework label.")
    parser.add_argument("--timeout-sec", type=int, default=None, help="Timeout seconds.")
    parser.add_argument("--report-path", default=None, help="Agent report path (overrides SCIMLOPSBENCH_REPORT).")
    parser.add_argument("--python", dest="cli_python", default=None, help="Explicit python executable to use.")
    parser.add_argument(
        "--python-script",
        default=None,
        help="Run resolved python with the given script path, passing args after --.",
    )
    parser.add_argument(
        "--python-module",
        default=None,
        help="Run resolved python -m <module>, passing args after --.",
    )
    parser.add_argument("--decision-reason", default="", help="Recorded in results.json meta.")
    parser.add_argument("--meta-json", default=None, help="Additional meta JSON object to merge.")
    parser.add_argument("--assets-json", default=None, help="Override assets JSON object.")
    parser.add_argument(
        "--no-assets-from-prepare",
        action="store_true",
        help="Do not auto-load assets from build_output/prepare/results.json.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Extra env var KEY=VALUE for the subprocess (repeatable).",
    )
    parser.add_argument(
        "--ensure-module",
        action="append",
        default=[],
        help="Ensure a python module is importable before running (module or module=package_spec). Repeatable.",
    )
    parser.add_argument("--skip", action="store_true", help="Mark stage as skipped (no command run).")
    parser.add_argument(
        "--skip-reason",
        default="unknown",
        choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"],
        help="Reason for skipping.",
    )
    parser.add_argument(
        "--print-python",
        action="store_true",
        help="Print resolved python and exit 0 (no results written).",
    )
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command after -- (or script/module args).")

    args = parser.parse_args()

    repo = _repo_root()
    report_path = _resolve_report_path(args.report_path)

    needs_python = bool(args.python_script or args.python_module or args.print_python)
    py_res: Optional[PythonResolution] = None
    py_err: Optional[str] = None
    if needs_python:
        py_res, py_err = resolve_python(args.cli_python, report_path)
        if args.print_python:
            if py_res is None:
                print("", end="")
                return 1
            print(py_res.python_path, end="")
            return 0

    if args.stage is None:
        parser.error("--stage is required unless --print-python is used")

    stage = args.stage
    out_dir = _stage_dir(stage)
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    timeout_sec = args.timeout_sec if args.timeout_sec is not None else _default_timeout_for_stage(stage)

    assets: Dict[str, Any]
    if args.assets_json:
        try:
            assets = json.loads(args.assets_json)
        except Exception:
            assets = _load_prepare_assets(repo)
    elif args.no_assets_from_prepare:
        assets = {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    else:
        assets = _load_prepare_assets(repo)

    meta: Dict[str, Any] = {
        "python": (py_res.python_path if py_res else ""),
        "python_resolution_source": (py_res.source if py_res else ""),
        "git_commit": _git_commit(repo),
        "env_vars": _select_env_vars(),
        "timestamp_utc": _utc_now_iso(),
        "decision_reason": args.decision_reason,
        "warnings": [py_res.warning] if (py_res and py_res.warning) else [],
    }
    if args.ensure_module:
        meta["ensure_module"] = list(args.ensure_module)

    if args.meta_json:
        try:
            extra_meta = json.loads(args.meta_json)
            if isinstance(extra_meta, dict):
                meta.update(extra_meta)
        except Exception:
            meta.setdefault("warnings", []).append("meta_json was provided but not valid JSON object")

    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": args.task,
        "command": "",
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": assets,
        "meta": meta,
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    if args.skip:
        results.update(
            {
                "status": "skipped",
                "skip_reason": args.skip_reason,
                "exit_code": 0,
                "command": "",
                "failure_category": "not_applicable",
                "error_excerpt": "",
            }
        )
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        log_path.write_text(f"[runner] stage skipped: {args.skip_reason}\n", encoding="utf-8")
        return 0

    if needs_python and py_res is None:
        results.update(
            {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "missing_report",
                "error_excerpt": f"Unable to resolve python: {py_err}. Report path tried: {report_path}",
                "command": "",
            }
        )
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        log_path.write_text(results["error_excerpt"] + "\n", encoding="utf-8")
        return 1

    cmd_tail = args.cmd
    if cmd_tail and cmd_tail[0] == "--":
        cmd_tail = cmd_tail[1:]

    if args.python_script:
        cmd = [py_res.python_path, args.python_script] + cmd_tail
    elif args.python_module:
        cmd = [py_res.python_path, "-m", args.python_module] + cmd_tail
    else:
        if not cmd_tail:
            results.update(
                {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": "args_unknown",
                    "error_excerpt": "No command provided (expected -- <cmd...> or --python-script/--python-module).",
                }
            )
            results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
            log_path.write_text(results["error_excerpt"] + "\n", encoding="utf-8")
            return 1
        cmd = cmd_tail

    cmd_str = _stringify_cmd(cmd)
    results["command"] = cmd_str

    env = dict(os.environ)
    for item in args.env:
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v

    executed_pieces: List[str] = []
    if args.ensure_module and py_res is not None:
        ok, details, fc, err_msg, executed = _ensure_python_modules(
            py_res.python_path,
            args.ensure_module,
            cwd=repo,
            env=env,
            log_path=log_path,
        )
        meta["ensure_module_results"] = details
        executed_pieces.extend(executed)
        if not ok:
            results.update(
                {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": fc or "deps",
                    "error_excerpt": _tail_text(log_path) or (err_msg or ""),
                }
            )
            if executed_pieces:
                results["command"] = " && ".join([p for p in executed_pieces if p])
            results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
            return 1

    if executed_pieces:
        executed_pieces.append(cmd_str)
        results["command"] = " && ".join([p for p in executed_pieces if p])
    start = time.time()
    cmd_rc, timed_out, runner_err = run_subprocess(cmd, cwd=repo, env=env, timeout_sec=timeout_sec, log_path=log_path)
    elapsed = time.time() - start
    meta["elapsed_sec"] = round(elapsed, 3)
    meta["command_returncode"] = cmd_rc
    if runner_err:
        meta.setdefault("warnings", []).append(runner_err)

    excerpt = _tail_text(log_path)

    if timed_out:
        results.update(
            {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "timeout",
                "error_excerpt": excerpt or (runner_err or ""),
            }
        )
    elif cmd_rc == 0:
        results.update(
            {
                "status": "success",
                "exit_code": 0,
                "failure_category": "unknown",
                "error_excerpt": "",
            }
        )
    else:
        failure_category = _infer_failure_category(excerpt, timed_out, cmd_rc)
        results.update(
            {
                "status": "failure",
                "exit_code": 1,
                "failure_category": failure_category,
                "error_excerpt": excerpt or (runner_err or ""),
            }
        )

    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if results["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
