#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root),
                text=True,
                stderr=subprocess.DEVNULL,
            )
            .strip()
            .strip()
        )
    except Exception:
        return ""


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def detect_cuda() -> Tuple[str, Dict[str, Any]]:
    observed: Dict[str, Any] = {
        "cuda_available": False,
        "gpu_count": 0,
    }

    try:
        import torch  # type: ignore

        observed["torch_import_ok"] = True
        observed["torch_version"] = getattr(torch, "__version__", "")
        observed["torch_cuda_version"] = getattr(getattr(torch, "version", None), "cuda", None)
        cuda_available = bool(getattr(torch.cuda, "is_available", lambda: False)())
        gpu_count = int(getattr(torch.cuda, "device_count", lambda: 0)()) if cuda_available else 0
        observed["cuda_available"] = bool(cuda_available and gpu_count > 0)
        observed["gpu_count"] = gpu_count
        if gpu_count > 0:
            try:
                observed["gpu_name_0"] = torch.cuda.get_device_name(0)
            except Exception:
                pass
        return "pytorch", observed
    except Exception as e:
        observed["torch_import_ok"] = False
        observed["torch_error"] = str(e)

    try:
        import tensorflow as tf  # type: ignore

        observed["tf_import_ok"] = True
        observed["tf_version"] = getattr(tf, "__version__", "")
        gpus = []
        try:
            gpus = tf.config.list_physical_devices("GPU")
        except Exception:
            gpus = []
        observed["gpu_count"] = len(gpus)
        observed["cuda_available"] = len(gpus) > 0
        return "tensorflow", observed
    except Exception as e:
        observed["tf_import_ok"] = False
        observed["tf_error"] = str(e)

    try:
        import jax  # type: ignore

        observed["jax_import_ok"] = True
        observed["jax_version"] = getattr(jax, "__version__", "")
        devices = []
        try:
            devices = list(jax.devices())
        except Exception:
            devices = []
        gpu_devices = [d for d in devices if getattr(d, "platform", "") == "gpu"]
        observed["gpu_count"] = len(gpu_devices)
        observed["cuda_available"] = len(gpu_devices) > 0
        return "jax", observed
    except Exception as e:
        observed["jax_import_ok"] = False
        observed["jax_error"] = str(e)

    return "unknown", observed


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    cmd_str = " ".join(shlex.quote(a) for a in sys.argv)

    framework, observed = detect_cuda()
    cuda_ok = bool(observed.get("cuda_available", False))
    gpu_count = int(observed.get("gpu_count", 0) or 0)

    status = "success" if cuda_ok else "failure"
    exit_code = 0 if cuda_ok else 1
    failure_category = "unknown" if cuda_ok else "runtime"
    error_excerpt = "" if cuda_ok else "CUDA not available (gpu_count=0 or framework import failed)."

    env_vars = {
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[cuda] started_utc={utc()}\n")
        log.write(f"[cuda] command={cmd_str}\n")
        log.write(f"[cuda] python_executable={sys.executable}\n")
        log.write(f"[cuda] python_version={platform.python_version()}\n")
        log.write(f"[cuda] framework={framework}\n")
        log.write(f"[cuda] cuda_available={cuda_ok}\n")
        log.write(f"[cuda] gpu_count={gpu_count}\n")
        for k, v in observed.items():
            log.write(f"[cuda] observed.{k}={v}\n")
        log.write(f"[cuda] ended_utc={utc()}\n")

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": cmd_str,
        "timeout_sec": 120,
        "framework": framework if framework in {"pytorch", "tensorflow", "jax"} else "unknown",
        "assets": {
            "dataset": {
                "path": str((root / "benchmark_assets" / "dataset").resolve()),
                "source": "not_applicable",
                "version": "unknown",
                "sha256": "",
            },
            "model": {
                "path": str((root / "benchmark_assets" / "model").resolve()),
                "source": "not_applicable",
                "version": "unknown",
                "sha256": "",
            },
        },
        "meta": {
            "python": sys.executable,
            "git_commit": git_commit(root),
            "env_vars": env_vars,
            "decision_reason": "Detect CUDA availability via torch/tensorflow/jax in the benchmark python environment.",
            "timestamp_utc": utc(),
        },
        "observed": observed,
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

