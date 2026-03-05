#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception as e:
        return None, f"read_error: {e}"
    try:
        parsed = json.loads(raw)
    except Exception:
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "invalid_json"
    return parsed, None


def _default_assets() -> Dict[str, Any]:
    return {
        "dataset": {"path": "unknown", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        "model": {"path": "unknown", "source": "unknown", "version": "unknown", "sha256": "unknown"},
    }


DETECTION_CODE = r"""
import json

out = {
  "framework": "unknown",
  "cuda_available": False,
  "gpu_count": 0,
  "details": {},
}

try:
  import torch
  out["framework"] = "pytorch"
  out["details"]["torch_version"] = getattr(torch, "__version__", None)
  out["cuda_available"] = bool(torch.cuda.is_available())
  out["gpu_count"] = int(torch.cuda.device_count() or 0)
except Exception as e:
  out["details"]["torch_error"] = str(e)

if out["framework"] == "unknown":
  try:
    import tensorflow as tf
    out["framework"] = "tensorflow"
    gpus = tf.config.list_physical_devices("GPU")
    out["gpu_count"] = int(len(gpus))
    out["cuda_available"] = out["gpu_count"] > 0
    out["details"]["tf_version"] = getattr(tf, "__version__", None)
  except Exception as e:
    out["details"]["tf_error"] = str(e)

if out["framework"] == "unknown":
  try:
    import jax
    out["framework"] = "jax"
    gpus = jax.devices("gpu")
    out["gpu_count"] = int(len(gpus))
    out["cuda_available"] = out["gpu_count"] > 0
    out["details"]["jax_version"] = getattr(jax, "__version__", None)
  except Exception as e:
    out["details"]["jax_error"] = str(e)

print(json.dumps(out))
"""


def resolve_report_path(cli: Optional[str]) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def resolve_python(cli_python: Optional[str], report_path: Path) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if cli_python:
        return cli_python, "cli", None
    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return env_python, "env", None

    report, err = _load_json(report_path)
    if err:
        return None, None, "missing_report"
    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        return None, None, "missing_report"
    if Path(python_path).is_file() and os.access(python_path, os.X_OK):
        return python_path, "report", None
    fallback = shutil.which("python") or shutil.which("python3")
    if fallback:
        return fallback, "path_fallback", f"python_path in report is not executable: {python_path}"
    return None, None, "missing_report"


def tail(text: str, max_lines: int = 220) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines).strip()
    return "\n".join(lines[-max_lines:]).strip()


def write_results(path: Path, payload: Dict[str, Any]) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python", dest="cli_python", default=None)
    parser.add_argument("--timeout-sec", type=int, default=120)
    args = parser.parse_args()

    out_dir = REPO_ROOT / "build_output" / "cuda"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    python_exe, python_source, python_err = resolve_python(args.cli_python, report_path)

    def log(msg: str) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(msg.rstrip() + "\n")

    if python_err or not python_exe:
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "cuda",
            "task": "check",
            "command": "",
            "timeout_sec": args.timeout_sec,
            "framework": "unknown",
            "assets": _default_assets(),
            "meta": {
                "python": "unknown",
                "git_commit": "unknown",
                "env_vars": {},
                "decision_reason": "CUDA check requires the report.json python_path (or --python override).",
                "timestamp_utc": _utc_timestamp(),
                "report_path": str(report_path),
            },
            "failure_category": "missing_report",
            "error_excerpt": "Missing or invalid report.json / python_path.",
        }
        write_results(results_path, payload)
        log(payload["error_excerpt"])
        return 1

    cmd = [python_exe, "-c", DETECTION_CODE]
    log(f"[cuda] python_exe={python_exe} (source={python_source}) report_path={report_path}")
    log(f"[cuda] cmd={' '.join(cmd)}")

    try:
        r = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=args.timeout_sec, check=False)
    except subprocess.TimeoutExpired:
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "cuda",
            "task": "check",
            "command": " ".join(cmd),
            "timeout_sec": args.timeout_sec,
            "framework": "unknown",
            "assets": _default_assets(),
            "meta": {
                "python": python_exe,
                "decision_reason": "Timed out while checking CUDA availability.",
                "timestamp_utc": _utc_timestamp(),
                "report_path": str(report_path),
                "python_resolution": {"source": python_source},
            },
            "failure_category": "timeout",
            "error_excerpt": "timeout",
        }
        write_results(results_path, payload)
        log("[cuda] TIMEOUT")
        return 1

    if r.stdout:
        log(r.stdout)
    if r.stderr:
        log(r.stderr)

    observed: Dict[str, Any] = {}
    framework = "unknown"
    cuda_available = False
    gpu_count = 0
    try:
        observed = json.loads((r.stdout or "{}").strip())
        if isinstance(observed, dict):
            framework = str(observed.get("framework", "unknown"))
            cuda_available = bool(observed.get("cuda_available", False))
            gpu_count = int(observed.get("gpu_count", 0) or 0)
        else:
            observed = {}
    except Exception as e:
        observed = {"parse_error": str(e), "raw": (r.stdout or "")[:4000]}

    status = "success" if cuda_available else "failure"
    exit_code = 0 if cuda_available else 1
    failure_category = "unknown" if cuda_available else "runtime"

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": " ".join(cmd),
        "timeout_sec": args.timeout_sec,
        "framework": framework if framework in {"pytorch", "tensorflow", "jax"} else "unknown",
        "assets": _default_assets(),
        "meta": {
            "python": python_exe,
            "git_commit": "unknown",
            "env_vars": {
                "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            },
            "decision_reason": "Detects CUDA availability via torch/tensorflow/jax in the reported python environment.",
            "timestamp_utc": _utc_timestamp(),
            "report_path": str(report_path),
            "python_resolution": {"source": python_source},
            "return_code": r.returncode,
        },
        "observed": {
            "cuda_available": cuda_available,
            "gpu_count": gpu_count,
            "raw": observed,
        },
        "failure_category": failure_category,
        "error_excerpt": "" if cuda_available else tail(log_path.read_text(encoding="utf-8", errors="replace")),
    }
    write_results(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

