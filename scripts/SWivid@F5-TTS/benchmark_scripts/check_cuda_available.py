#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).replace(microsecond=0).isoformat()


def safe_git_commit(root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return (cp.stdout or "").strip()
    except Exception:
        return ""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(DEFAULT_REPORT_PATH)


def read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception as e:
        return None, f"read_error:{type(e).__name__}:{e}"
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except Exception as e:
        return None, f"invalid_json:{type(e).__name__}:{e}"


def python_exec_ok(python_exe: str) -> Tuple[bool, str]:
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
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def resolve_python(cli_python: Optional[str], report_path: Path) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    meta: Dict[str, Any] = {"python_source": "unknown", "report_path": str(report_path)}
    if cli_python:
        meta["python_source"] = "cli"
        return cli_python, None, meta
    if os.environ.get("SCIMLOPSBENCH_PYTHON"):
        meta["python_source"] = "env"
        return os.environ["SCIMLOPSBENCH_PYTHON"], None, meta

    report, err = read_json(report_path)
    if err is not None or not isinstance(report, dict):
        return None, "missing_report", meta

    py = report.get("python_path")
    if not isinstance(py, str) or not py:
        return None, "missing_report", meta
    meta["python_source"] = "report"
    meta["reported_python_path"] = py
    return py, None, meta


def detect_cuda_via_python(python_exe: str, timeout_sec: int = 90) -> Tuple[Optional[dict], Optional[str]]:
    code = r"""
import json, sys

def try_torch():
    try:
        import torch
        return {
            "framework": "pytorch",
            "cuda_available": bool(torch.cuda.is_available()),
            "gpu_count": int(torch.cuda.device_count()),
            "torch_version": getattr(torch, "__version__", ""),
            "error": "",
        }
    except Exception as e:
        return {"framework": "pytorch", "error": f"{type(e).__name__}: {e}"}

def try_tf():
    try:
        import tensorflow as tf
        gpus = tf.config.list_physical_devices("GPU")
        return {
            "framework": "tensorflow",
            "cuda_available": len(gpus) > 0,
            "gpu_count": len(gpus),
            "tf_version": getattr(tf, "__version__", ""),
            "error": "",
        }
    except Exception as e:
        return {"framework": "tensorflow", "error": f"{type(e).__name__}: {e}"}

def try_jax():
    try:
        import jax
        devs = jax.devices()
        gpu_count = sum(1 for d in devs if getattr(d, "platform", "") == "gpu")
        return {
            "framework": "jax",
            "cuda_available": gpu_count > 0,
            "gpu_count": gpu_count,
            "jax_version": getattr(jax, "__version__", ""),
            "error": "",
        }
    except Exception as e:
        return {"framework": "jax", "error": f"{type(e).__name__}: {e}"}

res = {"python_executable": sys.executable}

torch_res = try_torch()
if torch_res.get("error"):
    tf_res = try_tf()
    if tf_res.get("error"):
        jax_res = try_jax()
        if jax_res.get("error"):
            res.update({"framework": "unknown", "cuda_available": False, "gpu_count": 0, "error": f"torch={torch_res.get('error')} | tf={tf_res.get('error')} | jax={jax_res.get('error')}"})
        else:
            res.update(jax_res)
    else:
        res.update(tf_res)
else:
    res.update(torch_res)

print(json.dumps(res))
"""
    try:
        cp = subprocess.run(
            [python_exe, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout_sec,
        )
        if cp.returncode != 0:
            return None, (cp.stderr or cp.stdout).strip()[-4000:]
        return json.loads(cp.stdout), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Check CUDA availability in the benchmark Python environment.")
    ap.add_argument("--python", default=None, help="Explicit python executable to use (highest priority).")
    ap.add_argument("--report-path", default=None, help="Agent report.json path override.")
    args = ap.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    command_str = f"{sys.executable} {Path(__file__).as_posix()} --report-path {report_path}"
    if args.python:
        command_str = f"{sys.executable} {Path(__file__).as_posix()} --python {args.python} --report-path {report_path}"

    env_vars = {
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
        "SCIMLOPSBENCH_PYTHON": "<set>" if os.environ.get("SCIMLOPSBENCH_PYTHON") else "",
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }

    base_assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

    python_exe, py_err, py_meta = resolve_python(args.python, report_path)
    if py_err is not None or not python_exe:
        msg = f"python_resolution_failed:{py_err}"
        write_text(log_path, msg + "\n")
        write_json(
            results_path,
            {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "cuda",
                "task": "check",
                "command": command_str,
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": base_assets,
                "meta": {
                    "python": "",
                    "bootstrap_python": sys.executable,
                    "git_commit": safe_git_commit(root),
                    "env_vars": env_vars,
                    "decision_reason": "Resolve python from agent report.json and check CUDA in that environment.",
                    "timestamp_utc": utc_now(),
                    **py_meta,
                },
                "observed": {"cuda_available": None, "gpu_count": None},
                "failure_category": "missing_report",
                "error_excerpt": msg,
            },
        )
        return 1

    ok, why = python_exec_ok(python_exe)
    if not ok:
        msg = f"python_not_executable:{python_exe}:{why}"
        write_text(log_path, msg + "\n")
        write_json(
            results_path,
            {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "cuda",
                "task": "check",
                "command": command_str,
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": base_assets,
                "meta": {
                    "python": python_exe,
                    "bootstrap_python": sys.executable,
                    "git_commit": safe_git_commit(root),
                    "env_vars": env_vars,
                    "decision_reason": "Resolve python from agent report.json and check CUDA in that environment.",
                    "timestamp_utc": utc_now(),
                    **py_meta,
                },
                "observed": {"cuda_available": None, "gpu_count": None},
                "failure_category": "path_hallucination",
                "error_excerpt": msg,
            },
        )
        return 1

    det, det_err = detect_cuda_via_python(python_exe)
    if det_err is not None or not isinstance(det, dict):
        msg = f"cuda_detection_failed:{det_err or 'unknown'}"
        write_text(log_path, msg + "\n")
        write_json(
            results_path,
            {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "cuda",
                "task": "check",
                "command": command_str,
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": base_assets,
                "meta": {
                    "python": python_exe,
                    "bootstrap_python": sys.executable,
                    "git_commit": safe_git_commit(root),
                    "env_vars": env_vars,
                    "decision_reason": "Detect CUDA availability using torch/tensorflow/jax inside resolved benchmark python.",
                    "timestamp_utc": utc_now(),
                    **py_meta,
                },
                "observed": {"cuda_available": None, "gpu_count": None},
                "failure_category": "runtime",
                "error_excerpt": msg,
            },
        )
        return 1

    framework = str(det.get("framework") or "unknown")
    cuda_available = bool(det.get("cuda_available")) if isinstance(det.get("cuda_available"), bool) else False
    gpu_count = int(det.get("gpu_count") or 0) if str(det.get("gpu_count", "")).isdigit() else 0
    backend_err = str(det.get("error") or "")

    status = "success" if cuda_available else "failure"
    exit_code = 0 if cuda_available else 1
    if exit_code == 0:
        failure_category = "unknown"
        error_excerpt = ""
    else:
        failure_category = "deps" if framework == "unknown" else "runtime"
        error_excerpt = backend_err or "CUDA not available"

    log_lines = [
        f"[cuda] bootstrap_python={sys.executable}",
        f"[cuda] resolved_python={python_exe}",
        f"[cuda] framework={framework}",
        f"[cuda] cuda_available={cuda_available}",
        f"[cuda] gpu_count={gpu_count}",
    ]
    if backend_err:
        log_lines.append(f"[cuda] backend_errors={backend_err}")
    write_text(log_path, "\n".join(log_lines) + "\n")

    write_json(
        results_path,
        {
            "status": status,
            "skip_reason": "unknown",
            "exit_code": exit_code,
            "stage": "cuda",
            "task": "check",
            "command": command_str,
            "timeout_sec": 120,
            "framework": framework if framework in {"pytorch", "tensorflow", "jax"} else "unknown",
            "assets": base_assets,
            "meta": {
                "python": python_exe,
                "bootstrap_python": sys.executable,
                "git_commit": safe_git_commit(root),
                "env_vars": env_vars,
                "decision_reason": "Detect CUDA availability using torch/tensorflow/jax inside resolved benchmark python.",
                "timestamp_utc": utc_now(),
                **py_meta,
            },
            "observed": {
                "cuda_available": cuda_available,
                "gpu_count": gpu_count,
                "python_executable": str(det.get("python_executable") or ""),
            },
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        },
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

