#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def get_git_commit(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def detect_cuda() -> Tuple[str, bool, int, str]:
    # Returns (framework, cuda_available, gpu_count, details)
    try:
        import torch  # type: ignore

        avail = bool(torch.cuda.is_available())
        count = int(torch.cuda.device_count()) if avail else int(torch.cuda.device_count())
        details = f"torch={getattr(torch, '__version__', '')}"
        return "pytorch", avail, count, details
    except Exception as e:
        torch_err = str(e)

    try:
        import tensorflow as tf  # type: ignore

        gpus = tf.config.list_physical_devices("GPU")
        avail = bool(gpus)
        count = len(gpus)
        details = f"tensorflow={getattr(tf, '__version__', '')}"
        return "tensorflow", avail, count, details
    except Exception as e:
        tf_err = str(e)

    try:
        import jax  # type: ignore

        gpus = [d for d in jax.devices() if getattr(d, "platform", "") == "gpu"]
        avail = bool(gpus)
        count = len(gpus)
        details = f"jax={getattr(jax, '__version__', '')}"
        return "jax", avail, count, details
    except Exception as e:
        jax_err = str(e)

    return "unknown", False, 0, f"torch_import_error={torch_err}; tf_import_error={tf_err}; jax_import_error={jax_err}"


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    logs = []

    def log(msg: str) -> None:
        line = msg.rstrip("\n")
        logs.append(line)
        print(line)

    framework, cuda_available, gpu_count, details = detect_cuda()
    log(f"framework={framework}")
    log(f"cuda_available={cuda_available}")
    log(f"gpu_count={gpu_count}")
    log(f"details={details}")

    status = "success" if cuda_available else "failure"
    exit_code = 0 if cuda_available else 1
    failure_category = "" if cuda_available else ("deps" if framework == "unknown" else "unknown")

    env_vars = {
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
    }

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": f"{sys.executable} {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "observed": {
            "cuda_available": cuda_available,
            "gpu_count": gpu_count,
            "details": details,
        },
        "meta": {
            "python": sys.executable,
            "git_commit": get_git_commit(root),
            "env_vars": env_vars,
            "decision_reason": "Detect CUDA via torch (preferred), else tensorflow, else jax.",
            "timestamp_utc": utc_timestamp(),
        },
        "failure_category": failure_category,
        "error_excerpt": "\n".join(logs[-200:]),
    }

    # Write log and results.
    log_path.write_text("\n".join(logs) + ("\n" if logs else ""), encoding="utf-8")
    write_json(results_path, payload)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

