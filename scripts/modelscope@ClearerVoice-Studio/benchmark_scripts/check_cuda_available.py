#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail(path: Path, n: int = 240) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:
        return None, f"invalid_json: {e}"


def resolve_report_path(cli: str) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT", "").strip()
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def resolve_python(cli_python: str, report_path: Path) -> Tuple[Optional[str], str]:
    if cli_python:
        return cli_python, "cli"
    env_py = os.environ.get("SCIMLOPSBENCH_PYTHON", "").strip()
    if env_py:
        return env_py, "env"
    report, _ = read_json(report_path)
    if report:
        py = str(report.get("python_path") or "").strip()
        if py:
            return py, "report"
    return None, "missing"


def run_snippet(python_exe: str, code: str, log_path: Path, timeout_sec: int = 60) -> Tuple[int, str]:
    cmd = [python_exe, "-c", code]
    with log_path.open("a", encoding="utf-8") as log_fp:
        log_fp.write(f"[cuda] cmd={cmd}\n")
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=timeout_sec)
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(out)
        return 0, out.strip()
    except subprocess.CalledProcessError as e:
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(e.output or "")
        return int(e.returncode or 1), (e.output or "").strip()
    except Exception as e:
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(f"[cuda] exception: {type(e).__name__}: {e}\n")
        return 1, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Check CUDA availability for torch/tensorflow/jax.")
    ap.add_argument("--report-path", default="", help="Override report.json path")
    ap.add_argument("--python", default="", help="Override python executable (otherwise uses report.json)")
    ap.add_argument("--timeout-sec", type=int, default=120)
    args = ap.parse_args()

    root = repo_root()
    stage_dir = root / "build_output" / "cuda"
    ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    report_path = resolve_report_path(args.report_path)
    python_exe, python_src = resolve_python(args.python, report_path)

    status = "failure"
    exit_code = 1
    skip_reason = "unknown"
    failure_category = "unknown"
    framework = "unknown"
    observed: Dict[str, Any] = {"cuda_available": False, "gpu_count": 0}

    command = ""

    if not python_exe:
        failure_category = "missing_report"
        command = ""
        payload = {
            "status": "failure",
            "skip_reason": skip_reason,
            "exit_code": 1,
            "stage": "cuda",
            "task": "check",
            "command": command,
            "timeout_sec": int(args.timeout_sec),
            "framework": framework,
            "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
            "meta": {
                "python": "",
                "git_commit": "",
                "env_vars": {"SCIMLOPSBENCH_REPORT": str(report_path)},
                "decision_reason": "Could not resolve python_path from report (or overrides).",
                "python_source": python_src,
                "timestamp_utc": utc_ts(),
            },
            "observed": observed,
            "failure_category": failure_category,
            "error_excerpt": tail(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    # Run detection in the configured python environment.
    detect_code = r"""
import json

obs = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "errors": {}}

def try_torch():
    import torch
    obs["framework"] = "pytorch"
    obs["cuda_available"] = bool(torch.cuda.is_available())
    obs["gpu_count"] = int(torch.cuda.device_count())

def try_tf():
    import tensorflow as tf
    obs["framework"] = "tensorflow"
    gpus = tf.config.list_physical_devices("GPU")
    obs["gpu_count"] = len(gpus)
    obs["cuda_available"] = obs["gpu_count"] > 0

def try_jax():
    import jax
    obs["framework"] = "jax"
    devs = jax.devices()
    obs["gpu_count"] = sum(1 for d in devs if getattr(d, "platform", "") == "gpu")
    obs["cuda_available"] = obs["gpu_count"] > 0

for name, fn in [("torch", try_torch), ("tensorflow", try_tf), ("jax", try_jax)]:
    try:
        fn()
        break
    except Exception as e:
        obs["errors"][name] = f"{type(e).__name__}: {e}"

print(json.dumps(obs))
"""

    command = f"{python_exe} -c <cuda-detect>"
    rc, out = run_snippet(python_exe, detect_code, log_path, timeout_sec=min(int(args.timeout_sec), 120))
    if rc != 0:
        failure_category = "deps"
        payload = {
            "status": "failure",
            "skip_reason": skip_reason,
            "exit_code": 1,
            "stage": "cuda",
            "task": "check",
            "command": command,
            "timeout_sec": int(args.timeout_sec),
            "framework": "unknown",
            "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
            "meta": {
                "python": python_exe,
                "git_commit": "",
                "env_vars": {"SCIMLOPSBENCH_REPORT": str(report_path)},
                "decision_reason": "Failed to run CUDA detection snippet.",
                "python_source": python_src,
                "timestamp_utc": utc_ts(),
            },
            "observed": observed,
            "failure_category": failure_category,
            "error_excerpt": tail(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    try:
        parsed = json.loads(out)
    except Exception:
        parsed = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "errors": {"parse": "invalid_json"}}

    framework = str(parsed.get("framework") or "unknown")
    observed = {
        "framework_probe": parsed,
        "cuda_available": bool(parsed.get("cuda_available")),
        "gpu_count": int(parsed.get("gpu_count") or 0),
    }

    if framework == "unknown":
        status = "failure"
        exit_code = 1
        failure_category = "deps"
    elif observed["cuda_available"] and observed["gpu_count"] > 0:
        status = "success"
        exit_code = 0
        failure_category = "none"
    else:
        status = "failure"
        exit_code = 1
        skip_reason = "insufficient_hardware"
        failure_category = "runtime"

    payload = {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": int(exit_code),
        "stage": "cuda",
        "task": "check",
        "command": command,
        "timeout_sec": int(args.timeout_sec),
        "framework": framework if framework in {"pytorch", "tensorflow", "jax"} else "unknown",
        "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
        "meta": {
            "python": python_exe,
            "git_commit": "",
            "env_vars": {"SCIMLOPSBENCH_REPORT": str(report_path)},
            "decision_reason": "CUDA availability probe via framework import.",
            "python_source": python_src,
            "timestamp_utc": utc_ts(),
        },
        "observed": observed,
        "failure_category": failure_category,
        "error_excerpt": tail(log_path) if status != "success" else "",
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

