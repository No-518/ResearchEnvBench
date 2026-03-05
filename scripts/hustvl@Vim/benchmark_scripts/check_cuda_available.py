#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, timeout=10)
            .strip()
        )
    except Exception:
        return ""


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path("/opt/scimlopsbench/report.json")


def is_executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def load_report(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(report_path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing_report:{report_path}"
    except Exception as e:
        return None, f"invalid_report:{report_path}:{type(e).__name__}:{e}"


def resolve_python(cli_python: Optional[str], report_path: Path) -> Tuple[Optional[str], Optional[str]]:
    if cli_python:
        p = Path(cli_python)
        if is_executable(p):
            return str(p), None
        return None, f"--python not executable: {cli_python}"

    env_py = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_py:
        p = Path(env_py)
        if is_executable(p):
            return str(p), None

    report, err = load_report(report_path)
    if report is None:
        return None, err or "missing_report"
    py = str(report.get("python_path", "") or "")
    if not py:
        return None, "python_path missing in report"
    p = Path(py)
    if not is_executable(p):
        return None, f"python_path not executable: {py}"
    return py, None


def probe_cuda(python_path: str, timeout_sec: int = 60) -> Tuple[Optional[Dict[str, Any]], str]:
    code = r"""
import json

out = {
  "framework": "unknown",
  "cuda_available": False,
  "gpu_count": 0,
  "details": {},
}

def _safe_str(e: Exception) -> str:
  return f"{type(e).__name__}: {e}"

try:
  import torch
  out["framework"] = "pytorch"
  out["details"]["torch_version"] = getattr(torch, "__version__", "")
  try:
    out["cuda_available"] = bool(torch.cuda.is_available())
    out["gpu_count"] = int(torch.cuda.device_count())
  except Exception as e:
    out["details"]["torch_cuda_error"] = _safe_str(e)
except Exception as e:
  out["details"]["torch_import_error"] = _safe_str(e)
  try:
    import tensorflow as tf
    out["framework"] = "tensorflow"
    out["details"]["tensorflow_version"] = getattr(tf, "__version__", "")
    try:
      gpus = tf.config.list_physical_devices("GPU")
      out["cuda_available"] = bool(gpus)
      out["gpu_count"] = int(len(gpus))
    except Exception as e2:
      out["details"]["tensorflow_cuda_error"] = _safe_str(e2)
  except Exception as e2:
    out["details"]["tensorflow_import_error"] = _safe_str(e2)
    try:
      import jax
      out["framework"] = "jax"
      out["details"]["jax_version"] = getattr(jax, "__version__", "")
      try:
        devs = jax.devices("gpu")
        out["cuda_available"] = bool(devs)
        out["gpu_count"] = int(len(devs))
      except Exception as e3:
        out["details"]["jax_cuda_error"] = _safe_str(e3)
    except Exception as e3:
      out["details"]["jax_import_error"] = _safe_str(e3)

print(json.dumps(out))
"""
    proc = subprocess.run(
        [python_path, "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=float(timeout_sec),
    )
    raw = (proc.stdout or "").strip()
    try:
        obj = json.loads(raw.splitlines()[-1]) if raw else None
        return obj, raw
    except Exception:
        return None, raw


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--report-path", default="")
    parser.add_argument("--python", default="")
    parser.add_argument("--timeout-sec", type=int, default=120)
    args = parser.parse_args()

    root = repo_root()
    out_dir = Path(args.out_dir) if args.out_dir else (root / "build_output" / "cuda")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path or None)
    python_path, py_err = resolve_python(args.python or None, report_path)

    framework = "unknown"
    observed: Dict[str, Any] = {
        "python_path": python_path or "",
        "cuda_available": False,
        "gpu_count": 0,
    }

    status = "failure"
    failure_category = "unknown"
    exit_code = 1

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"[cuda] timestamp_utc={utc_timestamp()}\n")
        logf.write(f"[cuda] report_path={report_path}\n")
        logf.write(f"[cuda] resolved_python={python_path or ''}\n")
        if py_err:
            logf.write(f"[cuda] python_resolution_error={py_err}\n")

        if not python_path:
            failure_category = "missing_report" if py_err and py_err.startswith(("missing_report", "invalid_report")) else "path_hallucination"
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "cuda",
                "task": "check",
                "command": "",
                "timeout_sec": int(args.timeout_sec),
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "meta": {
                    "python": "",
                    "git_commit": git_commit(root),
                    "env_vars": {
                        "SCIMLOPSBENCH_REPORT": str(report_path),
                        "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
                    },
                    "decision_reason": "Resolve python_path from agent report and probe CUDA availability via torch/tensorflow/jax.",
                },
                "observed": observed,
                "failure_category": failure_category,
                "error_excerpt": py_err or "python resolution failed",
            }
            write_json(results_path, payload)
            return 1

        obj, raw = probe_cuda(python_path, timeout_sec=min(60, int(args.timeout_sec)))
        logf.write("[cuda] probe_output_begin\n")
        logf.write(raw + ("\n" if not raw.endswith("\n") else ""))
        logf.write("[cuda] probe_output_end\n")

        if obj and isinstance(obj, dict):
            framework = str(obj.get("framework", "unknown") or "unknown")
            observed.update(
                {
                    "framework": framework,
                    "cuda_available": bool(obj.get("cuda_available", False)),
                    "gpu_count": int(obj.get("gpu_count", 0) or 0),
                    "details": obj.get("details", {}),
                }
            )
        else:
            failure_category = "runtime"

        if bool(observed.get("cuda_available")) and int(observed.get("gpu_count", 0) or 0) > 0:
            status = "success"
            failure_category = ""
            exit_code = 0
        else:
            status = "failure"
            if failure_category == "unknown":
                failure_category = "runtime"
            exit_code = 1

    error_excerpt = ""
    try:
        tail = _tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-240:]
        error_excerpt = "\n".join(tail).strip()
    except Exception:
        error_excerpt = ""

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": f"{python_path} -c <cuda_probe>",
        "timeout_sec": int(args.timeout_sec),
        "framework": framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": python_path,
            "git_commit": git_commit(root),
            "env_vars": {
                "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
                "SCIMLOPSBENCH_REPORT": str(report_path),
                "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            },
            "decision_reason": "Probe CUDA availability using the agent-reported python interpreter; try torch first, then tensorflow, then jax.",
            "timestamp_utc": utc_timestamp(),
        },
        "observed": observed,
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }
    write_json(results_path, payload)
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

