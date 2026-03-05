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
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _tail_lines(path: Path, max_lines: int = 200) -> str:
    if not path.exists():
        return ""
    dq: deque[str] = deque(maxlen=max_lines)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                dq.append(line.rstrip("\n"))
    except Exception:
        return ""
    return "\n".join(dq)


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


def _read_json_file(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"missing: {path}"
    except Exception as e:
        return None, f"read_failed: {path}: {e}"
    try:
        return json.loads(text), None
    except Exception as e:
        return None, f"invalid_json: {path}: {e}"


def _report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def _which_python_fallback() -> Optional[str]:
    for cand in ("python", "python3"):
        p = shutil.which(cand)
        if p:
            return p
    return None


def _is_executable_file(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except Exception:
        return False


def resolve_python(
    python_override: Optional[str],
    report_path: Path,
    requires_python: bool,
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """
    Resolution priority (highest to lowest):
      1) CLI --python
      2) Env var SCIMLOPSBENCH_PYTHON
      3) python_path from report.json
      4) Fallback python from PATH (record warning if used)

    Returns: (python_exe or None, meta, failure_category_if_hard_failure)
    """
    meta: Dict[str, Any] = {
        "report_path": str(report_path),
        "resolution": {
            "cli_python": python_override or "",
            "env_SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            "report_python_path": "",
            "fallback_python": "",
            "used": "",
            "warnings": [],
        },
    }

    # 1) CLI override
    if python_override:
        meta["resolution"]["used"] = "cli"
        if not _is_executable_file(python_override):
            return None, meta, "path_hallucination"
        return python_override, meta, None

    # 2) Env override
    env_py = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_py:
        meta["resolution"]["used"] = "env"
        if not _is_executable_file(env_py):
            return None, meta, "path_hallucination"
        return env_py, meta, None

    # 3) Report python_path
    report_obj, report_err = _read_json_file(report_path)
    if report_obj is None:
        if requires_python:
            meta["resolution"]["warnings"].append(report_err or "missing_report")
            return None, meta, "missing_report"
        # Non-python stage: proceed without report.
        meta["resolution"]["warnings"].append(report_err or "missing_report")
        return _which_python_fallback(), meta, None

    report_py = report_obj.get("python_path") if isinstance(report_obj, dict) else None
    if isinstance(report_py, str):
        meta["resolution"]["report_python_path"] = report_py
        if _is_executable_file(report_py):
            meta["resolution"]["used"] = "report"
            return report_py, meta, None
        meta["resolution"]["warnings"].append("report.python_path missing or not executable; falling back to PATH python")
    else:
        meta["resolution"]["warnings"].append("report.python_path missing; falling back to PATH python")

    # 4) Fallback PATH python
    fb = _which_python_fallback()
    meta["resolution"]["fallback_python"] = fb or ""
    if fb:
        meta["resolution"]["used"] = "fallback"
        meta["resolution"]["warnings"].append("using fallback python from PATH (recorded for validation)")
        return fb, meta, None

    return None, meta, "path_hallucination"


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _python_version(python_exe: str) -> str:
    try:
        out = subprocess.check_output(
            [python_exe, "-c", "import platform; print(platform.python_version())"],
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _collect_env_vars() -> Dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "NCCL_DEBUG",
        "NCCL_IB_DISABLE",
        "NCCL_P2P_DISABLE",
        "MASTER_ADDR",
        "MASTER_PORT",
        "WORLD_SIZE",
        "RANK",
        "LOCAL_RANK",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "TOKENIZERS_PARALLELISM",
        "HF_HOME",
        "HF_DATASETS_CACHE",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "PIP_CACHE_DIR",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "HF_AUTH_TOKEN",
        "HF_TOKEN",
    ]
    env: Dict[str, str] = {}
    for k in keys:
        if k in os.environ:
            env[k] = os.environ.get(k, "")
    return env


def _load_optional_json(path_str: str) -> Optional[dict]:
    if not path_str:
        return None
    path = Path(path_str)
    obj, err = _read_json_file(path)
    if obj is None:
        raise RuntimeError(err or f"invalid_json: {path}")
    if not isinstance(obj, dict):
        raise RuntimeError(f"invalid_json: {path}: expected object")
    return obj


def _default_timeout(stage: str) -> int:
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
    return defaults.get(stage, 600)


def _shlex_join(cmd: Sequence[str]) -> str:
    try:
        return shlex.join(list(cmd))
    except Exception:
        return " ".join(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="runner.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """
            Unified stage runner that:
              - resolves python interpreter (CLI/env/report/fallback)
              - executes a command (shell string via bash -lc, or argv JSON list)
              - captures stdout/stderr to <out-dir>/log.txt
              - writes <out-dir>/results.json even on failure
            """
        ).strip(),
    )
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--command", default="", help="Shell command to run; supports {python} placeholder")
    parser.add_argument("--command-json", default="", help="Path to JSON array argv; supports {python} element")
    parser.add_argument("--python", dest="python_override", default="")
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--requires-python", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--status", choices=["success", "failure", "skipped"], default="")
    parser.add_argument("--skip-reason", default="unknown")
    parser.add_argument("--failure-category", default="", help="Override failure_category when status=failure")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--assets-json", default="")
    parser.add_argument("--extra-meta-json", default="")
    parser.add_argument("--env", action="append", default=[], help="Extra env KEY=VALUE for the command")
    parser.add_argument("--print-python", action="store_true", help="Print resolved python path and exit")

    args = parser.parse_args()

    repo_root = _repo_root()
    out_dir = Path(args.out_dir)
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stage = args.stage
    task = args.task
    framework = args.framework
    timeout_sec = int(args.timeout_sec) if args.timeout_sec is not None else _default_timeout(stage)

    rpt_path = _report_path(args.report_path)

    python_exe, py_meta, py_fail_cat = resolve_python(
        python_override=args.python_override or None,
        report_path=rpt_path,
        requires_python=bool(args.requires_python),
    )

    if args.print_python:
        if python_exe is None:
            # Still emit a minimal results.json so callers have something to inspect.
            failure_category = py_fail_cat or "missing_report"
            with log_path.open("w", encoding="utf-8") as f:
                f.write(f"[{_utc_now_iso()}] failed to resolve python: {failure_category}\n")
                f.write(json.dumps(py_meta, indent=2, ensure_ascii=False) + "\n")
            results = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": stage,
                "task": task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": framework,
                "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
                "meta": {
                    "python": sys.executable,
                    "git_commit": _git_commit(repo_root),
                    "env_vars": _collect_env_vars(),
                    "decision_reason": args.decision_reason or "",
                    "python_resolution": py_meta,
                },
                "failure_category": failure_category,
                "error_excerpt": _tail_lines(log_path, 200),
            }
            results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return 1
        print(python_exe)
        return 0

    # Prepare assets/meta payloads
    assets_obj: Dict[str, Any] = {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}}
    extra_meta: Dict[str, Any] = {}
    try:
        if args.assets_json:
            loaded = _load_optional_json(args.assets_json)
            if loaded is not None:
                assets_obj = loaded.get("assets", loaded)  # allow either {"assets":{...}} or direct {"dataset":...}
        if args.extra_meta_json:
            loaded = _load_optional_json(args.extra_meta_json)
            if loaded is not None:
                extra_meta = loaded
    except Exception as e:
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"[{_utc_now_iso()}] failed to load assets/meta json: {e}\n")
        results = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": stage,
            "task": task,
            "command": args.command or "",
            "timeout_sec": timeout_sec,
            "framework": framework,
            "assets": assets_obj,
            "meta": {
                "python": python_exe or sys.executable,
                "git_commit": _git_commit(repo_root),
                "env_vars": _collect_env_vars(),
                "decision_reason": args.decision_reason or "",
                "python_resolution": py_meta,
                **extra_meta,
            },
            "failure_category": "invalid_json",
            "error_excerpt": _tail_lines(log_path, 200),
        }
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 1

    # If python is required and couldn't be resolved, fail now.
    if args.requires_python and python_exe is None:
        failure_category = py_fail_cat or "missing_report"
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"[{_utc_now_iso()}] failed to resolve python: {failure_category}\n")
            f.write(json.dumps(py_meta, indent=2, ensure_ascii=False) + "\n")

        results = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": stage,
            "task": task,
            "command": args.command or "",
            "timeout_sec": timeout_sec,
            "framework": framework,
            "assets": assets_obj,
            "meta": {
                "python": sys.executable,
                "git_commit": _git_commit(repo_root),
                "env_vars": _collect_env_vars(),
                "decision_reason": args.decision_reason or "",
                "python_resolution": py_meta,
                **extra_meta,
            },
            "failure_category": failure_category,
            "error_excerpt": _tail_lines(log_path, 200),
        }
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 1

    python_version = _python_version(python_exe) if python_exe else ""

    # Build command
    cmd_list: Optional[List[str]] = None
    cmd_str: str = ""

    if args.command_json:
        obj, err = _read_json_file(Path(args.command_json))
        if obj is None or not isinstance(obj, list) or not all(isinstance(x, str) for x in obj):
            with log_path.open("w", encoding="utf-8") as f:
                f.write(f"[{_utc_now_iso()}] invalid --command-json: {err or 'expected JSON array of strings'}\n")
            results = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": stage,
                "task": task,
                "command": "",
                "timeout_sec": timeout_sec,
                "framework": framework,
                "assets": assets_obj,
                "meta": {
                    "python": f"{python_exe} ({python_version})" if python_exe else sys.executable,
                    "git_commit": _git_commit(repo_root),
                    "env_vars": _collect_env_vars(),
                    "decision_reason": args.decision_reason or "",
                    "python_resolution": py_meta,
                    **extra_meta,
                },
                "failure_category": "args_unknown",
                "error_excerpt": _tail_lines(log_path, 200),
            }
            results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return 1
        cmd_list = list(obj)
        if python_exe:
            cmd_list = [python_exe if x == "{python}" else x.replace("{python}", python_exe) for x in cmd_list]
        cmd_str = _shlex_join(cmd_list)
    else:
        cmd_str = args.command.strip()
        if python_exe:
            cmd_str = cmd_str.replace("{python}", python_exe)

    # If explicit status override is 'skipped', do not run.
    explicit_status = args.status.strip()

    # Extra env
    env = os.environ.copy()
    for kv in args.env:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        env[k] = v

    # Always make cache directories writable & within repo if caller set them.
    # (No-op if not set.)
    stage_status: str = "failure"
    stage_exit_code: int = 1
    command_returncode: Optional[int] = None
    failure_category: str = args.failure_category.strip() or "unknown"
    skip_reason: str = "not_applicable"

    if explicit_status == "skipped":
        stage_status = "skipped"
        stage_exit_code = 0
        skip_reason = args.skip_reason or "unknown"
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"[{_utc_now_iso()}] stage skipped\n")
            f.write(f"stage={stage}\n")
            f.write(f"skip_reason={skip_reason}\n")
            if cmd_str:
                f.write(f"command={cmd_str}\n")
        results = {
            "status": stage_status,
            "skip_reason": skip_reason,
            "exit_code": stage_exit_code,
            "stage": stage,
            "task": task,
            "command": cmd_str,
            "timeout_sec": timeout_sec,
            "framework": framework,
            "assets": assets_obj,
            "meta": {
                "python": f"{python_exe} ({python_version})" if python_exe else sys.executable,
                "git_commit": _git_commit(repo_root),
                "env_vars": _collect_env_vars(),
                "decision_reason": args.decision_reason or "",
                "python_resolution": py_meta,
                **extra_meta,
            },
            "failure_category": "not_applicable",
            "error_excerpt": "",
        }
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 0

    if not cmd_str and not cmd_list:
        with log_path.open("w", encoding="utf-8") as f:
            f.write(f"[{_utc_now_iso()}] missing command\n")
        results = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": stage,
            "task": task,
            "command": "",
            "timeout_sec": timeout_sec,
            "framework": framework,
            "assets": assets_obj,
            "meta": {
                "python": f"{python_exe} ({python_version})" if python_exe else sys.executable,
                "git_commit": _git_commit(repo_root),
                "env_vars": _collect_env_vars(),
                "decision_reason": args.decision_reason or "",
                "python_resolution": py_meta,
                **extra_meta,
            },
            "failure_category": "args_unknown",
            "error_excerpt": _tail_lines(log_path, 200),
        }
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 1

    started = _utc_now_iso()
    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[{started}] runner start\n")
        log_f.write(f"stage={stage}\n")
        log_f.write(f"task={task}\n")
        log_f.write(f"timeout_sec={timeout_sec}\n")
        if python_exe:
            log_f.write(f"python_exe={python_exe}\n")
            if python_version:
                log_f.write(f"python_version={python_version}\n")
        log_f.write(f"command={cmd_str}\n\n")

        proc: Optional[subprocess.Popen[str]] = None
        timed_out = False
        t0 = time.time()
        try:
            if cmd_list is not None:
                proc = subprocess.Popen(
                    cmd_list,
                    cwd=str(repo_root),
                    env=env,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            else:
                proc = subprocess.Popen(
                    ["bash", "-lc", cmd_str],
                    cwd=str(repo_root),
                    env=env,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            proc.wait(timeout=timeout_sec)
            command_returncode = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            if proc is not None:
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
            command_returncode = 124
        except KeyboardInterrupt:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGINT)
                except Exception:
                    pass
            command_returncode = 130
        except Exception as e:
            log_f.write(f"\n[{_utc_now_iso()}] runner exception: {e}\n")
            command_returncode = 1
        finally:
            elapsed = time.time() - t0
            log_f.write(f"\n[{_utc_now_iso()}] runner end elapsed_sec={elapsed:.2f}\n")

    # Decide status
    if explicit_status in ("success", "failure"):
        stage_status = explicit_status
    else:
        stage_status = "success" if (command_returncode == 0) else "failure"

    if stage_status == "success":
        stage_exit_code = 0
        skip_reason = "not_applicable"
        failure_category = "not_applicable"
    else:
        stage_exit_code = 1
        skip_reason = "not_applicable"
        if args.failure_category.strip():
            failure_category = args.failure_category.strip()
        elif timed_out:
            failure_category = "timeout"
        else:
            failure_category = "runtime"

    error_excerpt = _tail_lines(log_path, 200) if stage_status == "failure" else ""
    if (
        stage_status == "failure"
        and stage in ("cpu", "single_gpu", "multi_gpu")
        and failure_category in ("unknown", "runtime")
        and not args.failure_category.strip()
    ):
        inferred = _infer_failure_category_from_log(error_excerpt)
        if inferred:
            failure_category = inferred

    results = {
        "status": stage_status,
        "skip_reason": skip_reason,
        "exit_code": stage_exit_code,
        "stage": stage,
        "task": task,
        "command": cmd_str,
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": assets_obj,
        "meta": {
            "python": f"{python_exe} ({python_version})" if python_exe else sys.executable,
            "git_commit": _git_commit(repo_root),
            "env_vars": _collect_env_vars(),
            "decision_reason": args.decision_reason or "",
            "python_resolution": py_meta,
            "command_returncode": command_returncode,
            **extra_meta,
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return stage_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
