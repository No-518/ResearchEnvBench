#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from bench_utils import REPO_ROOT, ensure_dir, get_git_commit, tail_lines, utc_timestamp, write_json


def resolve_report_path(cli_report_path: str) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path("/opt/scimlopsbench/report.json")


def resolve_python(cli_python: str, report_path: Path) -> Optional[str]:
    if cli_python:
        return cli_python
    if os.environ.get("SCIMLOPSBENCH_PYTHON"):
        return os.environ["SCIMLOPSBENCH_PYTHON"]
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        py = report.get("python_path") if isinstance(report, dict) else None
        return py if isinstance(py, str) and py else None
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="CUDA availability check (torch/tf/jax).")
    parser.add_argument("--report-path", default="", help="Override agent report path.")
    parser.add_argument("--python", default="", help="Override python executable.")
    args = parser.parse_args()

    stage = "cuda"
    out_dir = REPO_ROOT / "build_output" / stage
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    python_exe = resolve_python(args.python, report_path)

    log_lines = []
    log_lines.append(f"timestamp_utc={utc_timestamp()}")
    log_lines.append(f"report_path={report_path}")
    log_lines.append(f"python_executable={python_exe}")

    status = "failure"
    exit_code = 1
    failure_category = "unknown"
    framework = "unknown"
    observed: Dict[str, Any] = {"cuda_available": False, "gpu_count": 0}

    if not python_exe:
        failure_category = "missing_report"
        log_lines.append("ERROR: missing/invalid report python_path (or override via --python)")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        write_json(
            results_path,
            {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "cuda",
                "task": "check",
                "command": "",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "meta": {
                    "python": "",
                    "git_commit": get_git_commit(REPO_ROOT),
                    "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
                    "decision_reason": "CUDA check requires report python_path (or --python).",
                },
                "failure_category": failure_category,
                "error_excerpt": tail_lines(log_path),
                "observed": observed,
            },
        )
        return 1

    code = r"""
import json, sys

out = {"framework":"unknown","cuda_available": False, "gpu_count": 0}

try:
  import torch
  out["framework"] = "pytorch"
  out["torch_version"] = getattr(torch, "__version__", "")
  out["cuda_available"] = bool(torch.cuda.is_available())
  out["gpu_count"] = int(torch.cuda.device_count()) if out["cuda_available"] else int(torch.cuda.device_count())
  print(json.dumps(out))
  raise SystemExit(0)
except Exception as e:
  out["torch_error"] = f"{type(e).__name__}: {e}"

try:
  import tensorflow as tf
  out["framework"] = "tensorflow"
  out["tensorflow_version"] = getattr(tf, "__version__", "")
  gpus = tf.config.list_physical_devices("GPU")
  out["gpu_count"] = len(gpus)
  out["cuda_available"] = out["gpu_count"] > 0
  print(json.dumps(out))
  raise SystemExit(0)
except Exception as e:
  out["tensorflow_error"] = f"{type(e).__name__}: {e}"

try:
  import jax
  out["framework"] = "jax"
  out["jax_version"] = getattr(jax, "__version__", "")
  devices = jax.devices()
  out["gpu_count"] = sum(1 for d in devices if d.platform == "gpu")
  out["cuda_available"] = out["gpu_count"] > 0
  print(json.dumps(out))
  raise SystemExit(0)
except Exception as e:
  out["jax_error"] = f"{type(e).__name__}: {e}"

print(json.dumps(out))
"""

    cmd = [python_exe, "-c", code]
    cmd_str = " ".join([python_exe, "-c", "<cuda_probe>"])

    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        log_lines.append("subprocess_stdout:")
        log_lines.append(proc.stdout.rstrip())
        if proc.stderr:
            log_lines.append("subprocess_stderr:")
            log_lines.append(proc.stderr.rstrip())
        try:
            observed = json.loads(proc.stdout.strip() or "{}")
        except Exception:
            observed = {"framework": "unknown", "cuda_available": False, "gpu_count": 0}
        framework = observed.get("framework", "unknown")
        cuda_avail = bool(observed.get("cuda_available", False))
        if cuda_avail:
            status = "success"
            exit_code = 0
            failure_category = "not_applicable"
        else:
            status = "failure"
            exit_code = 1
            failure_category = "unknown"
    except subprocess.TimeoutExpired:
        log_lines.append("ERROR: timeout while checking CUDA")
        status = "failure"
        exit_code = 1
        failure_category = "timeout"
    except FileNotFoundError as e:
        log_lines.append(f"ERROR: python executable not found: {e}")
        status = "failure"
        exit_code = 1
        failure_category = "entrypoint_not_found"
    except Exception as e:
        log_lines.append(f"ERROR: {type(e).__name__}: {e}")
        status = "failure"
        exit_code = 1
        failure_category = "unknown"

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    write_json(
        results_path,
        {
            "status": status,
            "skip_reason": "not_applicable",
            "exit_code": exit_code,
            "stage": "cuda",
            "task": "check",
            "command": cmd_str,
            "timeout_sec": 120,
            "framework": framework if framework in ("pytorch", "tensorflow", "jax") else "unknown",
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "meta": {
                "python": python_exe,
                "git_commit": get_git_commit(REPO_ROOT),
                "env_vars": {k: os.environ.get(k, "") for k in ["CUDA_VISIBLE_DEVICES", "SCIMLOPSBENCH_REPORT"] if os.environ.get(k)},
                "decision_reason": "CUDA availability determined via report python_path in a subprocess probe.",
            },
            "failure_category": failure_category,
            "error_excerpt": tail_lines(log_path),
            "observed": observed,
        },
    )

    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

