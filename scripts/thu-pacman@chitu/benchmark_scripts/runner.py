#!/usr/bin/env python3
"""
Unified executor for benchmark stages.

This script is intentionally stdlib-only so it can run even when the benchmark
environment is partially broken.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
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


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def _now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _shell_join(cmd: list[str]) -> str:
    try:
        return shlex.join(cmd)
    except AttributeError:  # pragma: no cover
        return " ".join(shlex.quote(c) for c in cmd)


def _read_json(path: pathlib.Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tail_file(path: pathlib.Path, *, max_lines: int = 220, max_bytes: int = 512 * 1024) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_size = min(size, max_bytes)
            if read_size > 0:
                f.seek(-read_size, os.SEEK_END)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()[-max_lines:]
        return "\n".join(lines)
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"[runner] failed to tail log: {e}"


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

def _get_git_commit(repo_root: pathlib.Path) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            timeout=5,
            text=True,
        )
        return out.strip()
    except Exception:
        return None


def _is_executable_file(path: str) -> bool:
    try:
        p = pathlib.Path(path)
        return p.exists() and os.access(str(p), os.X_OK) and p.is_file()
    except Exception:
        return False


def _default_report_path(cli_report_path: Optional[str]) -> pathlib.Path:
    if cli_report_path:
        return pathlib.Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return pathlib.Path(env_path)
    return pathlib.Path("/opt/scimlopsbench/report.json")


def _fallback_python_from_path() -> Optional[str]:
    return shutil.which("python3") or shutil.which("python") or None


def _probe_python(python_exec: str, repo_root: pathlib.Path) -> dict[str, Any]:
    probe = {
        "executable": python_exec,
        "version": None,
        "ok": False,
        "error": None,
    }
    try:
        out = subprocess.check_output(
            [
                python_exec,
                "-c",
                "import json,platform,sys; print(json.dumps({'executable': sys.executable, 'version': platform.python_version()}))",
            ],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            timeout=10,
            text=True,
        ).strip()
        data = json.loads(out)
        probe["executable"] = data.get("executable") or python_exec
        probe["version"] = data.get("version")
        probe["ok"] = True
        return probe
    except Exception as e:
        probe["error"] = str(e)
        return probe


def _resolve_python_for_stage(
    *,
    cli_python: Optional[str],
    stage_requires_python: bool,
    report_path: pathlib.Path,
) -> tuple[Optional[str], dict[str, Any]]:
    """
    Resolution priority:
      1) CLI --python
      2) Env var SCIMLOPSBENCH_PYTHON
      3) python_path from report.json
      4) fallback python from PATH (only if report is valid JSON but python_path is missing/invalid)
    """
    meta: dict[str, Any] = {
        "source": None,
        "warning": None,
        "report_path": str(report_path),
    }

    if cli_python:
        meta["source"] = "cli"
        return cli_python, meta

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        meta["source"] = "env"
        return env_python, meta

    report_data = _read_json(report_path)
    if report_data is None:
        if stage_requires_python:
            meta["source"] = "report_missing_or_invalid"
            meta["warning"] = "report.json missing or invalid JSON; refusing to fall back without explicit --python/SCIMLOPSBENCH_PYTHON"
            return None, meta
        meta["source"] = "no_python_needed"
        return None, meta

    python_path = report_data.get("python_path")
    if not python_path:
        fb = _fallback_python_from_path()
        meta["source"] = "fallback"
        meta["warning"] = "report.json present but python_path missing; falling back to PATH python"
        return fb, meta

    if _is_executable_file(str(python_path)):
        meta["source"] = "report"
        return str(python_path), meta

    fb = _fallback_python_from_path()
    meta["source"] = "fallback"
    meta["warning"] = f"python_path from report is not an executable file: {python_path!r}; falling back to PATH python"
    return fb, meta


def _collect_env_vars(extra: dict[str, str]) -> dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "PYTHONPATH",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "NCCL_DEBUG",
        "NCCL_P2P_DISABLE",
        "OMP_NUM_THREADS",
    ]
    out: dict[str, str] = {}
    for k in keys:
        if k in os.environ:
            out[k] = os.environ[k]
    out.update(extra)
    return out


def _parse_env_kv(pairs: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            raise ValueError(f"--env expects KEY=VALUE, got: {p!r}")
        k, v = p.split("=", 1)
        env[k] = v
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a benchmark stage command with unified logging/results.")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--framework", default="unknown")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--assets-json", default=None)
    parser.add_argument("--extra-results-json", default=None, help="Optional JSON to merge into results.json.")

    parser.add_argument("--python", dest="cli_python", default=None)
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python-mode", action="store_true", help="Prepend resolved python to the command args.")

    parser.add_argument("--skip-reason", default=None, help="If set, skip execution and write results.json.")
    parser.add_argument("--env", action="append", default=[], help="Extra env var KEY=VALUE (repeatable).")

    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run (use `--` before it).")
    args = parser.parse_args()

    repo_root = _repo_root()
    stage = str(args.stage)
    task = str(args.task)
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else repo_root / "build_output" / stage
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = int(args.timeout_sec) if args.timeout_sec is not None else DEFAULT_TIMEOUTS_SEC.get(stage, 600)

    assets: dict[str, Any] = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    if args.assets_json:
        try:
            loaded = _read_json(pathlib.Path(args.assets_json))
            if isinstance(loaded, dict):
                assets = loaded  # expect shape matches
        except Exception:
            pass

    failure_category = "unknown"
    skip_reason = args.skip_reason or "unknown"
    status = "failure"
    exit_code = 1
    command_str = ""

    extra_env = _parse_env_kv(args.env)
    meta_env_vars = _collect_env_vars(extra_env)

    report_path = _default_report_path(args.report_path)
    python_exec: Optional[str] = None
    python_resolution: dict[str, Any] = {}

    try:
        # Ensure we always create/overwrite log.txt for the stage.
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"[runner] stage={stage} task={task} utc={_now_utc_iso()}\n")
            log.write(f"[runner] repo_root={repo_root}\n")
            log.write(f"[runner] out_dir={out_dir}\n")
            log.write(f"[runner] timeout_sec={timeout_sec}\n")
            log.flush()

        if args.skip_reason:
            status = "skipped"
            exit_code = 0
            failure_category = "unknown"
            command_str = ""
        else:
            cmd_args = list(args.command)
            if cmd_args and cmd_args[0] == "--":
                cmd_args = cmd_args[1:]
            if not cmd_args:
                failure_category = "args_unknown"
                raise RuntimeError("no command provided (missing `-- <command...>`)")

            stage_requires_python = bool(args.python_mode)
            python_exec, python_resolution = _resolve_python_for_stage(
                cli_python=args.cli_python,
                stage_requires_python=stage_requires_python,
                report_path=report_path,
            )

            if args.python_mode:
                if not python_exec:
                    failure_category = "missing_report"
                    raise RuntimeError(
                        f"cannot resolve python for stage {stage!r}; report_path={report_path}"
                    )
                cmd = [python_exec] + cmd_args
            else:
                cmd = cmd_args

            command_str = _shell_join(cmd)

            env = os.environ.copy()
            env.update(extra_env)
            env.setdefault("PYTHONUNBUFFERED", "1")

            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"[runner] command={command_str}\n")
                log.flush()

            # Run the command and capture stdout/stderr into log.txt
            proc = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(repo_root),
                    env=env,
                    stdout=log_path.open("ab"),
                    stderr=subprocess.STDOUT,
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None,
                )
                try:
                    exit_code = proc.wait(timeout=timeout_sec)
                except subprocess.TimeoutExpired:
                    failure_category = "timeout"
                    exit_code = 1
                    # Kill the whole process group (torchrun spawns children).
                    if proc and proc.poll() is None:
                        try:
                            if hasattr(os, "killpg"):
                                os.killpg(proc.pid, signal.SIGTERM)
                            else:
                                proc.terminate()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=10)
                        except Exception:
                            try:
                                if hasattr(os, "killpg"):
                                    os.killpg(proc.pid, signal.SIGKILL)
                                else:
                                    proc.kill()
                            except Exception:
                                pass
            except FileNotFoundError:
                failure_category = "entrypoint_not_found"
                exit_code = 1

            status = "success" if exit_code == 0 else "failure"
            if status == "failure" and failure_category == "unknown":
                failure_category = "runtime"

        # Merge optional stage-provided extra JSON into results
        extra_results: dict[str, Any] = {}
        if args.extra_results_json:
            loaded = _read_json(pathlib.Path(args.extra_results_json))
            if isinstance(loaded, dict):
                extra_results = loaded

        python_probe: Optional[dict[str, Any]] = None
        if python_exec:
            python_probe = _probe_python(python_exec, repo_root)

        error_excerpt = _tail_file(log_path)
        results: dict[str, Any] = {
            "status": status,
            "skip_reason": skip_reason,
            "exit_code": int(exit_code),
            "stage": stage,
            "task": task,
            "command": command_str,
            "timeout_sec": int(timeout_sec),
            "framework": str(args.framework),
            "assets": assets,
            "meta": {
                "python": (
                    f"{python_probe.get('executable')} ({python_probe.get('version')})"
                    if python_probe and python_probe.get("ok")
                    else (python_exec or "")
                ),
                "git_commit": _get_git_commit(repo_root),
                "env_vars": meta_env_vars,
                "decision_reason": str(args.decision_reason),
                "timestamp_utc": _now_utc_iso(),
                "python_resolution": python_resolution,
            },
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }

        # Shallow merge extra fields (extras win)
        for k, v in extra_results.items():
            results[k] = v

        if (
            results.get("status") == "failure"
            and stage in ("cpu", "single_gpu", "multi_gpu")
            and results.get("failure_category") in ("unknown", "runtime")
        ):
            inferred = _infer_failure_category_from_log(error_excerpt)
            if inferred:
                results["failure_category"] = inferred

        _write_json(results_path, results)
        return 0 if status in ("success", "skipped") else 1

    except Exception as e:
        # Best-effort: write results.json even if runner blew up.
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[runner] internal error: {e}\n")
        results = {
            "status": "failure",
            "skip_reason": skip_reason,
            "exit_code": 1,
            "stage": stage,
            "task": task,
            "command": command_str,
            "timeout_sec": int(timeout_sec),
            "framework": str(args.framework),
            "assets": assets,
            "meta": {
                "python": python_exec or "",
                "git_commit": _get_git_commit(repo_root),
                "env_vars": meta_env_vars,
                "decision_reason": str(args.decision_reason),
                "timestamp_utc": _now_utc_iso(),
                "python_resolution": python_resolution,
            },
            "failure_category": failure_category if failure_category != "unknown" else "unknown",
            "error_excerpt": _tail_file(log_path),
        }
        try:
            _write_json(results_path, results)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
