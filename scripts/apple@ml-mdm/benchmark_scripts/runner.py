#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_git_commit(repo_root: Path) -> str:
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


def _is_executable_file(path: str) -> bool:
    try:
        p = Path(path)
        return p.is_file() and os.access(str(p), os.X_OK)
    except Exception:
        return False


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = text.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)


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


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, ""
    except FileNotFoundError:
        return None, "missing_report"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "unknown"


def _resolve_report_path(cli_report_path: str) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(DEFAULT_REPORT_PATH)


@dataclass(frozen=True)
class PythonResolution:
    python: str
    source: str  # cli | env | report | path_fallback
    warning: str
    report_path: str
    report_loaded: bool


def resolve_python(
    *,
    cli_python: str,
    cli_report_path: str,
    require_report_if_no_cli_python: bool,
) -> Tuple[Optional[PythonResolution], str]:
    if cli_python:
        return (
            PythonResolution(
                python=cli_python,
                source="cli",
                warning="",
                report_path=str(_resolve_report_path(cli_report_path)),
                report_loaded=False,
            ),
            "",
        )

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON", "")
    if env_python:
        return (
            PythonResolution(
                python=env_python,
                source="env",
                warning="",
                report_path=str(_resolve_report_path(cli_report_path)),
                report_loaded=False,
            ),
            "",
        )

    report_path = _resolve_report_path(cli_report_path)
    report, report_err = _load_json(report_path)
    if report is None:
        if require_report_if_no_cli_python:
            return None, report_err or "missing_report"
        fallback = shutil_which_python()
        if not fallback:
            return None, "missing_report"
        return (
            PythonResolution(
                python=fallback,
                source="path_fallback",
                warning=f"report_unavailable:{report_err or 'missing_report'}; fell back to python from PATH",
                report_path=str(report_path),
                report_loaded=False,
            ),
            "",
        )

    python_path = str(report.get("python_path") or "").strip()
    if python_path:
        if _is_executable_file(python_path):
            return (
                PythonResolution(
                    python=python_path,
                    source="report",
                    warning="",
                    report_path=str(report_path),
                    report_loaded=True,
                ),
                "",
            )
        fallback = shutil_which_python()
        if not fallback:
            return (
                PythonResolution(
                    python=python_path,
                    source="report",
                    warning="python_path_not_executable_and_no_path_fallback",
                    report_path=str(report_path),
                    report_loaded=True,
                ),
                "",
            )
        return (
            PythonResolution(
                python=fallback,
                source="path_fallback",
                warning=f"python_path_not_executable:{python_path}; fell back to python from PATH",
                report_path=str(report_path),
                report_loaded=True,
            ),
            "",
        )

    fallback = shutil_which_python()
    if not fallback:
        if require_report_if_no_cli_python:
            return None, "missing_report"
        return None, "missing_report"
    return (
        PythonResolution(
            python=fallback,
            source="path_fallback",
            warning="python_path_missing_in_report; fell back to python from PATH",
            report_path=str(report_path),
            report_loaded=True,
        ),
        "",
    )


def shutil_which_python() -> str:
    for name in ("python", "python3"):
        try:
            out = subprocess.check_output(
                ["bash", "-lc", f"command -v {shlex.quote(name)}"],
                cwd=str(REPO_ROOT),
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
            ).strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def _python_version_string(python_exe: str) -> str:
    try:
        out = subprocess.check_output(
            [python_exe, "-c", "import platform; print(platform.python_version())"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
        return out
    except Exception:
        return ""


def _default_env_vars_to_record() -> Dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "TRANSFORMERS_OFFLINE",
        "HF_DATASETS_OFFLINE",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "PIP_CACHE_DIR",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "SCIMLOPSBENCH_MULTI_GPU_DEVICES",
        "SCIMLOPSBENCH_MULTI_GPU_NPROC",
    ]
    out: Dict[str, str] = {}
    for k in keys:
        if k in os.environ:
            out[k] = os.environ.get(k, "")
    return out


def _empty_assets() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _assets_from_prepare(prepare_results_path: Path) -> Tuple[Dict[str, Any], str]:
    data, err = _load_json(prepare_results_path)
    if data is None:
        return _empty_assets(), err or "missing_stage_results"
    assets = data.get("assets")
    if not isinstance(assets, dict):
        return _empty_assets(), "invalid_json"
    return assets, ""


def _format_command_for_results(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(x) for x in argv)


def write_results_json(out_dir: Path, payload: Dict[str, Any]) -> None:
    _ensure_dir(out_dir)
    results_path = out_dir / "results.json"
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_subprocess(
    *,
    argv: Sequence[str],
    cwd: Path,
    env: Dict[str, str],
    timeout_sec: int,
    log_path: Path,
) -> Tuple[int, bool, str]:
    _ensure_dir(log_path.parent)
    with log_path.open("ab") as log_f:
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=str(cwd),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            msg = f"entrypoint_not_found: {e}"
            log_f.write((msg + "\n").encode("utf-8", errors="replace"))
            return 127, False, msg
        except Exception as e:
            msg = f"failed_to_start_process: {e}"
            log_f.write((msg + "\n").encode("utf-8", errors="replace"))
            return 1, False, msg

        timed_out = False
        err_msg = ""
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            err_msg = f"timeout_after_{timeout_sec}_sec"
            try:
                os.killpg(proc.pid, 9)
            except Exception:
                proc.kill()
        ret = proc.returncode if proc.returncode is not None else 1
        return ret, timed_out, err_msg


def _build_base_results(
    *,
    stage: str,
    task: str,
    command: str,
    timeout_sec: int,
    framework: str,
    assets: Dict[str, Any],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": task,
        "command": command,
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": assets,
        "meta": meta,
        "failure_category": "unknown",
        "error_excerpt": "",
    }


def _cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Unified executor for env-bench benchmark stages.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_res = sub.add_parser("resolve-python", help="Print resolved python executable path.")
    p_res.add_argument("--python", default="", help="Explicit python executable (highest priority).")
    p_res.add_argument("--report-path", default="", help="Override report.json path.")
    p_res.add_argument("--allow-missing-report", action="store_true")

    p_run = sub.add_parser("run", help="Run a command and write build_output/<stage>/{log.txt,results.json}.")
    p_run.add_argument("--stage", required=True)
    p_run.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    p_run.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    p_run.add_argument("--timeout-sec", type=int, required=True)
    p_run.add_argument("--out-dir", default="")
    p_run.add_argument("--python", default="")
    p_run.add_argument("--report-path", default="")
    p_run.add_argument("--assets-from-prepare", default="")
    p_run.add_argument("--decision-reason", default="")
    p_run.add_argument("--env", action="append", default=[], help="Extra env var KEY=VALUE (repeatable).")
    p_run.add_argument("--require-report", action="store_true", help="Fail if report missing/invalid and --python not set.")
    p_run.add_argument("--skip-reason-on-success", default="", help="Set skip_reason field even on success/failure.")
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="Command to execute (after --).")

    p_write = sub.add_parser("write", help="Write results.json (and log.txt) without running a command.")
    p_write.add_argument("--stage", required=True)
    p_write.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    p_write.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    p_write.add_argument("--timeout-sec", type=int, required=True)
    p_write.add_argument("--out-dir", default="")
    p_write.add_argument("--python", default="")
    p_write.add_argument("--report-path", default="")
    p_write.add_argument("--assets-from-prepare", default="")
    p_write.add_argument("--decision-reason", default="")
    p_write.add_argument("--status", required=True, choices=["success", "failure", "skipped"])
    p_write.add_argument("--skip-reason", default="unknown", choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"])
    p_write.add_argument("--failure-category", default="unknown")
    p_write.add_argument("--message", default="")
    p_write.add_argument("--require-report", action="store_true", help="Fail if report missing/invalid and --python not set.")
    return p


def main(argv: Sequence[str]) -> int:
    args = _cli().parse_args(argv)

    if args.cmd == "resolve-python":
        resolved, err = resolve_python(
            cli_python=args.python,
            cli_report_path=args.report_path,
            require_report_if_no_cli_python=not args.allow_missing_report,
        )
        if resolved is None:
            print("", end="")
            return 1
        print(resolved.python)
        return 0

    stage = args.stage
    out_dir = Path(args.out_dir) if args.out_dir else (REPO_ROOT / "build_output" / stage)
    log_path = out_dir / "log.txt"

    resolved, err = resolve_python(
        cli_python=getattr(args, "python", ""),
        cli_report_path=getattr(args, "report_path", ""),
        require_report_if_no_cli_python=bool(getattr(args, "require_report", False)),
    )

    if resolved is None:
        _ensure_dir(out_dir)
        log_path.write_text(
            f"runner.py: failed to resolve python (error={err})\n", encoding="utf-8"
        )
        payload = _build_base_results(
            stage=stage,
            task=args.task,
            command="",
            timeout_sec=int(args.timeout_sec),
            framework=args.framework,
            assets=_empty_assets(),
            meta={
                "python": f"{sys.executable} ({platform.python_version()})",
                "git_commit": _read_git_commit(REPO_ROOT),
                "env_vars": _default_env_vars_to_record(),
                "decision_reason": getattr(args, "decision_reason", ""),
                "timestamp_utc": _now_utc_iso(),
                "python_resolution_error": err,
            },
        )
        payload["status"] = "failure"
        payload["exit_code"] = 1
        payload["failure_category"] = "missing_report" if err in ("missing_report", "invalid_json") else "unknown"
        payload["error_excerpt"] = _tail_lines(log_path)
        write_results_json(out_dir, payload)
        return 1

    resolved_python = resolved.python
    resolved_python_version = _python_version_string(resolved_python)
    git_commit = _read_git_commit(REPO_ROOT)

    assets = _empty_assets()
    assets_source_error = ""
    if getattr(args, "assets_from_prepare", ""):
        assets, assets_source_error = _assets_from_prepare(Path(args.assets_from_prepare))

    meta: Dict[str, Any] = {
        "python": f"{resolved_python} ({resolved_python_version})" if resolved_python_version else resolved_python,
        "git_commit": git_commit,
        "env_vars": _default_env_vars_to_record(),
        "decision_reason": getattr(args, "decision_reason", ""),
        "timestamp_utc": _now_utc_iso(),
        "python_resolution": {
            "python": resolved_python,
            "source": resolved.source,
            "warning": resolved.warning,
            "report_path": resolved.report_path,
            "report_loaded": resolved.report_loaded,
        },
    }
    if assets_source_error:
        meta["assets_warning"] = f"failed_to_load_assets_from_prepare:{assets_source_error}"

    if args.cmd == "write":
        _ensure_dir(out_dir)
        if args.message:
            log_path.write_text(args.message.rstrip() + "\n", encoding="utf-8")
        else:
            if not log_path.exists():
                log_path.write_text("", encoding="utf-8")

        payload = _build_base_results(
            stage=stage,
            task=args.task,
            command="",
            timeout_sec=int(args.timeout_sec),
            framework=args.framework,
            assets=assets,
            meta=meta,
        )
        payload["status"] = args.status
        payload["skip_reason"] = args.skip_reason
        payload["exit_code"] = 0 if args.status in ("success", "skipped") else 1
        payload["failure_category"] = args.failure_category
        payload["error_excerpt"] = _tail_lines(log_path)
        write_results_json(out_dir, payload)
        return 0 if payload["exit_code"] == 0 else 1

    # run
    cmd_argv: List[str] = list(getattr(args, "command", []))
    if cmd_argv and cmd_argv[0] == "--":
        cmd_argv = cmd_argv[1:]
    if not cmd_argv:
        _ensure_dir(out_dir)
        log_path.write_text("runner.py: no command provided\n", encoding="utf-8")
        payload = _build_base_results(
            stage=stage,
            task=args.task,
            command="",
            timeout_sec=int(args.timeout_sec),
            framework=args.framework,
            assets=assets,
            meta=meta,
        )
        payload["failure_category"] = "args_unknown"
        payload["error_excerpt"] = _tail_lines(log_path)
        write_results_json(out_dir, payload)
        return 1

    cmd_str = _format_command_for_results(cmd_argv)
    meta["command_argv"] = cmd_argv

    env = os.environ.copy()
    for item in getattr(args, "env", []):
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        env[k] = v
        meta["env_vars"][k] = v

    ret, timed_out, err_msg = run_subprocess(
        argv=cmd_argv,
        cwd=REPO_ROOT,
        env=env,
        timeout_sec=int(args.timeout_sec),
        log_path=log_path,
    )

    payload = _build_base_results(
        stage=stage,
        task=args.task,
        command=cmd_str,
        timeout_sec=int(args.timeout_sec),
        framework=args.framework,
        assets=assets,
        meta=meta,
    )
    payload["meta"]["command_exit_code"] = ret
    payload["meta"]["timed_out"] = timed_out
    payload["meta"]["runner_error"] = err_msg

    if timed_out:
        payload["status"] = "failure"
        payload["exit_code"] = 1
        payload["failure_category"] = "timeout"
    elif ret == 0:
        payload["status"] = "success"
        payload["exit_code"] = 0
        payload["failure_category"] = "unknown"
    else:
        payload["status"] = "failure"
        payload["exit_code"] = 1
        payload["failure_category"] = "entrypoint_not_found" if ret == 127 else "runtime"

    payload["skip_reason"] = getattr(args, "skip_reason_on_success", "") or "not_applicable"
    payload["error_excerpt"] = _tail_lines(log_path)
    if (
        payload["status"] == "failure"
        and stage in ("cpu", "single_gpu", "multi_gpu")
        and payload.get("failure_category") in ("unknown", "runtime")
    ):
        inferred = _infer_failure_category_from_log(payload["error_excerpt"])
        if inferred:
            payload["failure_category"] = inferred
    write_results_json(out_dir, payload)
    return 0 if payload["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
