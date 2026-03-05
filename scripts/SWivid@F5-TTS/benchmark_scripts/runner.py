#!/usr/bin/env python3
import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"
DEFAULT_TIMEOUTS_SEC = {
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


def _utc_now() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def _selected_env_vars(env: Dict[str, str]) -> Dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "HF_DATASETS_CACHE",
        "TRANSFORMERS_CACHE",
        "XDG_CACHE_HOME",
        "TORCH_HOME",
        "WANDB_DIR",
        "WANDB_MODE",
        "WANDB_API_KEY",
        "PYTHONPATH",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "HF_TOKEN",
        "HUGGINGFACE_HUB_TOKEN",
    ]
    out: Dict[str, str] = {}
    for k in keys:
        if k in env:
            if any(s in k for s in ["TOKEN", "KEY", "PASSWORD", "SECRET"]):
                out[k] = "<set>"
            else:
                out[k] = env[k]
    return out


def _safe_tail_text(path: Path, max_lines: int = 220, max_bytes: int = 256_000) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes), os.SEEK_SET)
            except Exception:
                pass
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        tail = lines[-max_lines:]
        return "\n".join(tail)
    except Exception as e:
        return f"(failed to read log tail: {type(e).__name__}: {e})"


def _git_commit(repo_root: Path) -> Optional[str]:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if cp.returncode != 0:
            return None
        return cp.stdout.strip() or None
    except Exception:
        return None


def _read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_file"
    except Exception as e:
        return None, f"read_error:{type(e).__name__}:{e}"
    try:
        return json.loads(raw), None
    except Exception as e:
        return None, f"invalid_json:{type(e).__name__}:{e}"


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(DEFAULT_REPORT_PATH)


def _resolve_python(
    *,
    cli_python: Optional[str],
    cli_report_path: Optional[str],
    require_report: bool,
) -> Tuple[Optional[str], List[str], Optional[str], Path]:
    warnings: List[str] = []
    report_path = _resolve_report_path(cli_report_path)

    if cli_python:
        return cli_python, warnings, None, report_path
    if os.environ.get("SCIMLOPSBENCH_PYTHON"):
        return os.environ["SCIMLOPSBENCH_PYTHON"], warnings, None, report_path

    report, err = _read_json(report_path)
    if err is not None:
        if require_report:
            return None, warnings, "missing_report", report_path
        warnings.append(f"report_unavailable:{err}")
        py = shutil.which("python") or shutil.which("python3")
        if py:
            warnings.append("python_fallback=PATH")
            return py, warnings, None, report_path
        return None, warnings, "missing_report", report_path

    py = None
    if isinstance(report, dict):
        py = report.get("python_path")
    if not py or not isinstance(py, str):
        if require_report:
            return None, warnings, "missing_report", report_path
        warnings.append("python_path_missing_in_report")
        py2 = shutil.which("python") or shutil.which("python3")
        if py2:
            warnings.append("python_fallback=PATH")
            return py2, warnings, None, report_path
        return None, warnings, "missing_report", report_path

    return py, warnings, None, report_path


def _python_exec_ok(python_exe: str) -> Tuple[bool, Optional[str]]:
    try:
        cp = subprocess.run(
            [python_exe, "-c", "import sys; print(sys.executable)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
        if cp.returncode != 0:
            return False, (cp.stderr or cp.stdout).strip()[-2000:]
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _augment_cache_env(env: Dict[str, str], repo_root: Path) -> Dict[str, str]:
    out = dict(env)
    cache_root = repo_root / "benchmark_assets" / "cache"
    out.setdefault("XDG_CACHE_HOME", str(cache_root / "xdg"))
    out.setdefault("HF_HOME", str(cache_root / "huggingface"))
    out.setdefault("HF_HUB_CACHE", str(cache_root / "huggingface" / "hub"))
    out.setdefault("HUGGINGFACE_HUB_CACHE", str(cache_root / "huggingface" / "hub"))
    out.setdefault("HF_DATASETS_CACHE", str(cache_root / "huggingface" / "datasets"))
    out.setdefault("TRANSFORMERS_CACHE", str(cache_root / "huggingface" / "transformers"))
    out.setdefault("TORCH_HOME", str(cache_root / "torch"))
    out.setdefault("WANDB_DIR", str(cache_root / "wandb"))
    out.setdefault("WANDB_MODE", out.get("WANDB_MODE", "offline"))
    out.setdefault("PYTHONUNBUFFERED", "1")
    out.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    src_dir = repo_root / "src"
    if src_dir.exists():
        prev = out.get("PYTHONPATH", "")
        out["PYTHONPATH"] = f"{src_dir}{os.pathsep}{prev}" if prev else str(src_dir)
    return out


def _run_subprocess(
    *,
    cmd: List[str],
    cwd: Path,
    env: Dict[str, str],
    log_path: Path,
    timeout_sec: int,
) -> Tuple[int, bool, Optional[str]]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[runner] cwd={cwd}\n")
        log_f.write(f"[runner] cmd={' '.join(cmd)}\n")
        log_f.write(f"[runner] timeout_sec={timeout_sec}\n")
        log_f.flush()
        start = time.time()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                rc = proc.wait(timeout=timeout_sec)
                return rc, False, None
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                return 124, True, f"timeout after {timeout_sec}s"
        except FileNotFoundError as e:
            return 127, False, f"not_found:{e}"
        except Exception as e:
            elapsed = time.time() - start
            return 1, False, f"runner_error:{type(e).__name__}:{e} (elapsed={elapsed:.2f}s)"


def _load_assets(repo_root: Path) -> Dict[str, Any]:
    prepare_results = repo_root / "build_output" / "prepare" / "results.json"
    payload, err = _read_json(prepare_results)
    if err or not isinstance(payload, dict):
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    assets = payload.get("assets")
    if not isinstance(assets, dict):
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    ds = assets.get("dataset") if isinstance(assets.get("dataset"), dict) else {}
    mdl = assets.get("model") if isinstance(assets.get("model"), dict) else {}
    return {
        "dataset": {
            "path": str(ds.get("path", "")),
            "source": str(ds.get("source", "")),
            "version": str(ds.get("version", "")),
            "sha256": str(ds.get("sha256", "")),
        },
        "model": {
            "path": str(mdl.get("path", "")),
            "source": str(mdl.get("source", "")),
            "version": str(mdl.get("version", "")),
            "sha256": str(mdl.get("sha256", "")),
        },
    }


def _write_results(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_resolve_python(args: argparse.Namespace) -> int:
    py, warnings, err, report_path = _resolve_python(
        cli_python=args.python,
        cli_report_path=args.report_path,
        require_report=args.require_report,
    )
    payload = {
        "python": py or "",
        "warnings": warnings,
        "error": err or "",
        "report_path": str(report_path),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 0 if (py and not err) else 1


def cmd_run(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    out_dir = repo_root / (args.out_dir or f"build_output/{args.stage}")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = args.timeout_sec or DEFAULT_TIMEOUTS_SEC.get(args.stage, 600)

    env = _augment_cache_env(os.environ.copy(), repo_root)
    for kv in args.env or []:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        env[k] = v

    base = {
        "status": "failure",
        "skip_reason": args.skip_reason or "unknown",
        "exit_code": 1,
        "stage": args.stage,
        "task": args.task,
        "command": "",
        "timeout_sec": int(timeout_sec),
        "framework": args.framework or "unknown",
        "assets": _load_assets(repo_root),
        "meta": {
            "python": "",
            "git_commit": _git_commit(repo_root) or "",
            "env_vars": _selected_env_vars(env),
            "decision_reason": args.decision_reason or "",
            "timestamp_utc": _utc_now(),
            "warnings": [],
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    if args.skip:
        base["status"] = "skipped"
        base["exit_code"] = 0
        base["failure_category"] = "unknown"
        base["command"] = args.command or ""
        _write_results(results_path, base)
        if not log_path.exists():
            log_path.write_text("[runner] skipped\n", encoding="utf-8")
        return 0

    run_mode = None
    cmd: List[str] = []
    if args.py_module:
        run_mode = "py_module"
    elif args.py_script:
        run_mode = "py_script"
    elif args.cmd:
        run_mode = "cmd"
        cmd = args.cmd
    else:
        base["failure_category"] = "args_unknown"
        base["error_excerpt"] = "No command provided. Use --py-module, --py-script, or --cmd after --."
        _write_results(results_path, base)
        log_path.write_text(base["error_excerpt"] + "\n", encoding="utf-8")
        return 1

    if run_mode in {"py_module", "py_script"}:
        py, warnings, err, _report_path = _resolve_python(
            cli_python=args.python,
            cli_report_path=args.report_path,
            require_report=True,
        )
        if err is not None or not py:
            base["failure_category"] = "missing_report"
            base["meta"]["python"] = ""
            base["meta"]["warnings"] = warnings
            base["error_excerpt"] = f"python_resolution_failed: {err} (report_path={_report_path})"
            _write_results(results_path, base)
            log_path.write_text(base["error_excerpt"] + "\n", encoding="utf-8")
            return 1

        ok, why = _python_exec_ok(py)
        if not ok:
            base["failure_category"] = "path_hallucination"
            base["meta"]["python"] = py
            base["meta"]["warnings"] = warnings
            base["error_excerpt"] = f"python_not_executable: {why}"
            _write_results(results_path, base)
            log_path.write_text(base["error_excerpt"] + "\n", encoding="utf-8")
            return 1

        base["meta"]["python"] = py
        base["meta"]["warnings"] = warnings

        # Ensure external binaries from the resolved environment (e.g. ffmpeg/ffprobe in conda/venv) are discoverable.
        try:
            py_dir = str(Path(py).resolve().parent)
            env["PATH"] = f"{py_dir}{os.pathsep}{env.get('PATH','')}"
        except Exception:
            pass

        if run_mode == "py_module":
            cmd = [py, "-u", "-m", args.py_module] + (args.py_args or [])
        else:
            script_path = str((repo_root / args.py_script).resolve()) if not os.path.isabs(args.py_script) else args.py_script
            cmd = [py, "-u", script_path] + (args.py_args or [])

    base["command"] = args.command or " ".join(cmd)

    rc, timed_out, runner_err = _run_subprocess(cmd=cmd, cwd=repo_root, env=env, log_path=log_path, timeout_sec=timeout_sec)

    if timed_out:
        base["status"] = "failure"
        base["exit_code"] = 1
        base["failure_category"] = "timeout"
    elif rc == 0:
        base["status"] = "success"
        base["exit_code"] = 0
        base["failure_category"] = "unknown"
    else:
        base["status"] = "failure"
        base["exit_code"] = 1
        if rc == 127 or (runner_err and runner_err.startswith("not_found:")):
            base["failure_category"] = "entrypoint_not_found"
        else:
            base["failure_category"] = "runtime"

    if runner_err:
        base["meta"]["warnings"].append(runner_err)

    base["error_excerpt"] = _safe_tail_text(log_path)
    _write_results(results_path, base)
    return 0 if base["exit_code"] == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="runner.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Unified benchmark runner that writes log.txt and results.json for each stage.",
        epilog=textwrap.dedent(
            """\
Examples:
  python benchmark_scripts/runner.py resolve-python
  python benchmark_scripts/runner.py run --stage cpu --task infer --framework pytorch --py-module f5_tts.infer.infer_cli --py-args --help
  python benchmark_scripts/runner.py run --stage cuda --task check --py-script benchmark_scripts/check_cuda_available.py
"""
        ),
    )
    sub = p.add_subparsers(dest="subcmd", required=True)

    rp = sub.add_parser("resolve-python", help="Resolve python executable according to benchmark rules.")
    rp.add_argument("--python", default=None, help="Explicit python executable (highest priority).")
    rp.add_argument("--report-path", default=None, help="Path to agent report.json.")
    rp.add_argument("--require-report", action="store_true", help="Fail if report missing/invalid (default: false).")
    rp.set_defaults(func=cmd_resolve_python)

    run = sub.add_parser("run", help="Run a stage command and write build_output/<stage> artifacts.")
    run.add_argument("--stage", required=True, help="Stage name, e.g. prepare|cpu|cuda|single_gpu|multi_gpu|env_size|hallucination.")
    run.add_argument("--task", required=True, help="Task, e.g. download|train|infer|check|validate.")
    run.add_argument("--framework", default="unknown", help="Framework: pytorch|tensorflow|jax|unknown.")
    run.add_argument("--out-dir", default=None, help="Output dir relative to repo root (default: build_output/<stage>).")
    run.add_argument("--timeout-sec", type=int, default=None, help="Timeout in seconds.")
    run.add_argument("--python", default=None, help="Explicit python executable for python-mode runs.")
    run.add_argument("--report-path", default=None, help="Path to agent report.json.")
    run.add_argument("--env", action="append", default=[], help="Extra env var KEY=VALUE (repeatable).")
    run.add_argument("--decision-reason", default=None, help="Short explanation for chosen entrypoint/params.")
    run.add_argument("--command", default=None, help="Command string to record (defaults to joined argv).")
    run.add_argument("--skip", action="store_true", help="Mark stage as skipped without running.")
    run.add_argument("--skip-reason", default="unknown", help="Skip reason for skipped status.")
    run.add_argument("--py-module", default=None, help="Run a python module with resolved python.")
    run.add_argument("--py-script", default=None, help="Run a python script path with resolved python.")
    run.add_argument("--py-args", nargs=argparse.REMAINDER, help="Arguments for --py-module/--py-script.")
    run.add_argument("--", dest="cmd_sep", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run after -- (cmd-mode).")
    run.set_defaults(func=cmd_run)

    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if getattr(args, "cmd", None) and args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    if getattr(args, "cmd", None) and args.cmd:
        args.cmd = [c for c in args.cmd if c]
    else:
        args.cmd = []
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
