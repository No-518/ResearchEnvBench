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
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"

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


def _now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _base_assets() -> dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_last_lines(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= max_lines:
                step = block if size >= block else size
                size -= step
                f.seek(size, os.SEEK_SET)
                data = f.read(step) + data
            lines = data.splitlines()[-max_lines:]
            return "\n".join(l.decode("utf-8", errors="replace") for l in lines)
    except Exception:
        return ""


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


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT", "")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


class ReportResolutionError(RuntimeError):
    pass


class PythonResolutionError(RuntimeError):
    pass


def _resolve_python_executable(cli_python: str | None, cli_report_path: str | None) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {
        "python_resolution": {
            "source": "",
            "report_path": "",
            "report_loaded": False,
        },
        "warnings": [],
    }

    if cli_python:
        meta["python_resolution"]["source"] = "cli"
        py = cli_python
        if not (Path(py).exists() and os.access(py, os.X_OK)):
            raise PythonResolutionError(f"--python does not exist or is not executable: {py}")
        return py, meta

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON", "")
    if env_python:
        meta["python_resolution"]["source"] = "env:SCIMLOPSBENCH_PYTHON"
        py = env_python
        if not (Path(py).exists() and os.access(py, os.X_OK)):
            raise PythonResolutionError(
                "SCIMLOPSBENCH_PYTHON does not exist or is not executable: " + py
            )
        return py, meta

    report_path = _resolve_report_path(cli_report_path)
    meta["python_resolution"]["report_path"] = str(report_path)
    try:
        report = _read_json(report_path)
        meta["python_resolution"]["report_loaded"] = True
    except FileNotFoundError:
        raise ReportResolutionError(f"Missing report: {report_path}")
    except json.JSONDecodeError as e:
        raise ReportResolutionError(f"Invalid JSON in report: {report_path}: {e}")
    except Exception as e:
        raise ReportResolutionError(f"Failed to read report: {report_path}: {e}")

    py = str(report.get("python_path") or "")
    if py:
        meta["python_resolution"]["source"] = "report:python_path"
        if not (Path(py).exists() and os.access(py, os.X_OK)):
            raise PythonResolutionError(f"python_path does not exist or is not executable: {py}")
        return py, meta

    fallback = shutil.which("python3") or shutil.which("python")
    if not fallback:
        raise PythonResolutionError("python_path missing in report and no python found in PATH")
    meta["python_resolution"]["source"] = "path_fallback"
    meta["warnings"].append("python_path missing in report; using python from PATH as last resort")
    return fallback, meta


def _preflight_python(python_exe: str, env: dict[str, str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [python_exe, "-c", "import sys; print(sys.executable)"],
            env=env,
            cwd=str(_repo_root()),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            return True, proc.stdout.strip()
        return False, (proc.stderr or proc.stdout).strip()
    except Exception as e:
        return False, str(e)


def _detect_python_version(python_exe: str, env: dict[str, str]) -> str:
    try:
        proc = subprocess.run(
            [python_exe, "-c", "import platform; print(platform.python_version())"],
            env=env,
            cwd=str(_repo_root()),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return ""


def _min_gpu_check(python_exe: str, env: dict[str, str]) -> tuple[bool, int, str]:
    code = r"""
import json
out = {"ok": False, "gpu_count": 0, "error": ""}
try:
    import torch
    out["gpu_count"] = int(torch.cuda.device_count())
    out["ok"] = True
except Exception as e:
    out["error"] = repr(e)
print(json.dumps(out))
"""
    try:
        proc = subprocess.run(
            [python_exe, "-c", code],
            env=env,
            cwd=str(_repo_root()),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            return False, 0, (proc.stderr or proc.stdout).strip()
        payload = json.loads(proc.stdout.strip() or "{}")
        if not payload.get("ok"):
            return False, int(payload.get("gpu_count") or 0), str(payload.get("error") or "")
        return True, int(payload.get("gpu_count") or 0), ""
    except Exception as e:
        return False, 0, repr(e)


def _cmd_to_str(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _load_assets_from(path: str | None) -> dict[str, Any]:
    if not path:
        return _base_assets()
    p = Path(path)
    if not p.exists():
        return _base_assets()
    try:
        data = _read_json(p)
        assets = data.get("assets")
        if isinstance(assets, dict) and "dataset" in assets and "model" in assets:
            return assets
    except Exception:
        pass
    return _base_assets()


def _write_stage_results(
    out_dir: Path,
    stage: str,
    task: str,
    status: str,
    skip_reason: str,
    exit_code: int,
    command: str,
    timeout_sec: int,
    framework: str,
    assets: dict[str, Any],
    meta: dict[str, Any],
    failure_category: str,
    error_excerpt: str,
) -> None:
    payload: dict[str, Any] = {
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
    _write_json(out_dir / "results.json", payload)


def _resolve_python_only(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Resolve benchmark python interpreter.")
    parser.add_argument("--python", default=None)
    parser.add_argument("--report-path", default=None)
    ns = parser.parse_args(argv)
    try:
        py, _meta = _resolve_python_executable(ns.python, ns.report_path)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1
    print(py)
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "resolve-python":
        return _resolve_python_only(argv[1:])

    parser = argparse.ArgumentParser(description="Unified stage runner for env-bench.")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--framework", default="unknown")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--assets-from", default=None)
    parser.add_argument("--python", default=None)
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--use-python", action="store_true")
    parser.add_argument("--min-gpus", type=int, default=None)
    parser.add_argument("--failure-category", default="")
    parser.add_argument("--skip", action="store_true")
    parser.add_argument("--skip-reason", default="unknown")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")
    ns = parser.parse_args(argv)

    repo_root = _repo_root()
    stage = ns.stage
    task = ns.task
    framework = ns.framework
    timeout_sec = ns.timeout_sec if ns.timeout_sec is not None else DEFAULT_TIMEOUTS_SEC.get(stage, 600)

    out_dir = Path(ns.out_dir) if ns.out_dir else (repo_root / "build_output" / stage)
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"

    env = os.environ.copy()
    cache_root = repo_root / "benchmark_assets" / "cache"
    _ensure_dir(cache_root)
    # Prefer keeping incidental caches under benchmark_assets/cache.
    env["XDG_CACHE_HOME"] = str(cache_root / "xdg")
    env["HF_HOME"] = str(cache_root / "hf_home")
    env["TORCH_HOME"] = str(cache_root / "torch_home")
    env["TORCH_EXTENSIONS_DIR"] = str(cache_root / "torch_extensions")
    env["MPLCONFIGDIR"] = str(cache_root / "mpl")
    env["NUMBA_CACHE_DIR"] = str(cache_root / "numba")
    env["HOME"] = str(cache_root / "home")
    env["TMPDIR"] = str(cache_root / "tmp")
    env["TMP"] = env["TMPDIR"]
    env["TEMP"] = env["TMPDIR"]

    _ensure_dir(Path(env["XDG_CACHE_HOME"]))
    _ensure_dir(Path(env["HF_HOME"]))
    _ensure_dir(Path(env["TORCH_HOME"]))
    _ensure_dir(Path(env["TORCH_EXTENSIONS_DIR"]))
    _ensure_dir(Path(env["MPLCONFIGDIR"]))
    _ensure_dir(Path(env["NUMBA_CACHE_DIR"]))
    _ensure_dir(Path(env["HOME"]))
    _ensure_dir(Path(env["TMPDIR"]))

    env_overrides: dict[str, str] = {}
    for item in ns.env:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        env[k] = v
        env_overrides[k] = v

    assets = _load_assets_from(ns.assets_from)
    meta: dict[str, Any] = {
        "python": sys.version.split()[0],
        "git_commit": _git_commit(repo_root),
        "timestamp_utc": _now_utc_iso(),
        "env_vars": env_overrides,
        "decision_reason": ns.decision_reason,
    }

    # Ensure results.json is always written.
    status = "failure"
    skip_reason = "not_applicable"
    exit_code = 1
    failure_category = ns.failure_category or "unknown"
    error_excerpt = ""
    command_str = ""

    try:
        with log_path.open("w", encoding="utf-8") as log_f:
            if ns.skip:
                status = "skipped"
                skip_reason = ns.skip_reason or "unknown"
                exit_code = 0
                command_str = ""
                failure_category = ""
                log_f.write(f"[runner] stage skipped: {stage} (reason={skip_reason})\n")
                _write_stage_results(
                    out_dir=out_dir,
                    stage=stage,
                    task=task,
                    status=status,
                    skip_reason=skip_reason,
                    exit_code=exit_code,
                    command=command_str,
                    timeout_sec=timeout_sec,
                    framework=framework,
                    assets=assets,
                    meta=meta,
                    failure_category=failure_category,
                    error_excerpt="",
                )
                return 0

            cmd = ns.command
            if cmd and cmd[0] == "--":
                cmd = cmd[1:]
            if not cmd:
                log_f.write("[runner] missing command (expected after --)\n")
                failure_category = ns.failure_category or "args_unknown"
                _write_stage_results(
                    out_dir=out_dir,
                    stage=stage,
                    task=task,
                    status="failure",
                    skip_reason="not_applicable",
                    exit_code=1,
                    command="",
                    timeout_sec=timeout_sec,
                    framework=framework,
                    assets=assets,
                    meta=meta,
                    failure_category=failure_category,
                    error_excerpt="missing command",
                )
                return 1

            resolved_python = ""
            python_meta: dict[str, Any] = {}
            if ns.use_python or ns.min_gpus is not None:
                try:
                    resolved_python, python_meta = _resolve_python_executable(ns.python, ns.report_path)
                    ok, detail = _preflight_python(resolved_python, env)
                    if not ok:
                        raise PythonResolutionError(f"python preflight failed: {detail}")
                    meta["python_resolution"] = python_meta.get("python_resolution", {})
                    if python_meta.get("warnings"):
                        meta.setdefault("warnings", []).extend(python_meta["warnings"])
                    meta["resolved_python"] = resolved_python
                    meta["resolved_python_version"] = _detect_python_version(resolved_python, env)
                except ReportResolutionError as e:
                    log_f.write(f"[runner] {e}\n")
                    failure_category = ns.failure_category or "missing_report"
                    _write_stage_results(
                        out_dir=out_dir,
                        stage=stage,
                        task=task,
                        status="failure",
                        skip_reason="not_applicable",
                        exit_code=1,
                        command="",
                        timeout_sec=timeout_sec,
                        framework=framework,
                        assets=assets,
                        meta=meta,
                        failure_category=failure_category,
                        error_excerpt=str(e),
                    )
                    return 1
                except PythonResolutionError as e:
                    log_f.write(f"[runner] {e}\n")
                    failure_category = ns.failure_category or "path_hallucination"
                    _write_stage_results(
                        out_dir=out_dir,
                        stage=stage,
                        task=task,
                        status="failure",
                        skip_reason="not_applicable",
                        exit_code=1,
                        command="",
                        timeout_sec=timeout_sec,
                        framework=framework,
                        assets=assets,
                        meta=meta,
                        failure_category=failure_category,
                        error_excerpt=str(e),
                    )
                    return 1

            if ns.min_gpus is not None:
                ok, gpu_count, err = _min_gpu_check(resolved_python, env)
                meta["gpu_check"] = {"ok": ok, "gpu_count": gpu_count, "min_gpus": ns.min_gpus, "error": err}
                if not ok:
                    log_f.write(f"[runner] failed to query GPU count: {err}\n")
                    failure_category = ns.failure_category or "runtime"
                    _write_stage_results(
                        out_dir=out_dir,
                        stage=stage,
                        task=task,
                        status="failure",
                        skip_reason="not_applicable",
                        exit_code=1,
                        command="",
                        timeout_sec=timeout_sec,
                        framework=framework,
                        assets=assets,
                        meta=meta,
                        failure_category=failure_category,
                        error_excerpt="failed to query GPU count: " + (err or "unknown"),
                    )
                    return 1
                if gpu_count < ns.min_gpus:
                    log_f.write(
                        f"[runner] insufficient GPUs: observed {gpu_count}, need >= {ns.min_gpus}\n"
                    )
                    status = "failure"
                    skip_reason = "not_applicable"
                    exit_code = 1
                    command_str = ""
                    failure_category = ns.failure_category or "runtime"
                    return 1

            if ns.use_python:
                cmd = [resolved_python, *cmd]

            command_str = _cmd_to_str(cmd)
            log_f.write(f"[runner] command: {command_str}\n")
            log_f.flush()

            proc = subprocess.Popen(cmd, cwd=str(repo_root), env=env, stdout=log_f, stderr=log_f, text=True)
            try:
                proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=30)
                except Exception:
                    pass
                status = "failure"
                exit_code = 1
                failure_category = ns.failure_category or "timeout"
            else:
                rc = int(proc.returncode or 0)
                if rc == 0:
                    status = "success"
                    exit_code = 0
                    failure_category = ""
                else:
                    status = "failure"
                    exit_code = 1
                    failure_category = ns.failure_category or "runtime"

    finally:
        if status == "failure":
            error_excerpt = _read_last_lines(log_path, max_lines=240)
            if stage in ("cpu", "single_gpu", "multi_gpu") and failure_category in ("runtime", "unknown"):
                inferred = _infer_failure_category_from_log(error_excerpt)
                if inferred:
                    failure_category = inferred
        _write_stage_results(
            out_dir=out_dir,
            stage=stage,
            task=task,
            status=status,
            skip_reason=skip_reason,
            exit_code=exit_code,
            command=command_str,
            timeout_sec=timeout_sec,
            framework=framework,
            assets=assets,
            meta=meta,
            failure_category=failure_category,
            error_excerpt=error_excerpt,
        )

    return 0 if status in ("success", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
