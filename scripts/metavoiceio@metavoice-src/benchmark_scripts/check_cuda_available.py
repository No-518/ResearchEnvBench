#!/usr/bin/env python3
from __future__ import annotations

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
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT, text=True, timeout=5)
            .strip()
        )
    except Exception:  # noqa: BLE001
        return ""


def tail(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:  # noqa: BLE001
        return ""


def detect_cuda() -> Tuple[str, Dict[str, Any]]:
    # Returns (framework, observed)
    try:
        import torch  # type: ignore

        return (
            "pytorch",
            {
                "cuda_available": bool(torch.cuda.is_available()),
                "gpu_count": int(torch.cuda.device_count()),
                "device_names": [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())] if torch.cuda.is_available() else [],
                "torch_version": getattr(torch, "__version__", ""),
            },
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        import tensorflow as tf  # type: ignore

        gpus = tf.config.list_physical_devices("GPU")
        return (
            "tensorflow",
            {
                "cuda_available": bool(gpus),
                "gpu_count": len(gpus),
                "device_names": [d.name for d in gpus],
                "tensorflow_version": getattr(tf, "__version__", ""),
            },
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        import jax  # type: ignore

        devices = jax.devices()
        gpus = [d for d in devices if d.platform == "gpu"]
        return (
            "jax",
            {
                "cuda_available": bool(gpus),
                "gpu_count": len(gpus),
                "device_names": [str(d) for d in gpus],
                "jax_version": getattr(jax, "__version__", ""),
            },
        )
    except Exception:  # noqa: BLE001
        pass

    return "unknown", {"cuda_available": False, "gpu_count": 0, "device_names": []}


def main() -> int:
    root = repo_root()
    stage_dir = root / "build_output" / "cuda"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    framework, observed = "unknown", {}
    exit_code = 1
    status = "failure"
    failure_category = "runtime"

    try:
        framework, observed = detect_cuda()
        cuda_ok = bool(observed.get("cuda_available", False))
        exit_code = 0 if cuda_ok else 1
        status = "success" if cuda_ok else "failure"
        failure_category = "unknown" if cuda_ok else "runtime"

        log_path.write_text(
            json.dumps({"framework": framework, "observed": observed}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        log_path.write_text(f"Exception during CUDA detection: {exc}\n", encoding="utf-8")
        status = "failure"
        exit_code = 1
        failure_category = "runtime"

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": "benchmark_scripts/check_cuda_available.py",
        "timeout_sec": 120,
        "framework": framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": sys.version.split()[0],
            "git_commit": git_commit(root),
            "env_vars": {k: os.environ.get(k, "") for k in ["CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES"]},
            "decision_reason": "Detect CUDA availability via torch/tensorflow/jax in the active Python environment.",
            "timestamp_utc": utc_timestamp(),
        },
        "observed": observed,
        "failure_category": failure_category,
        "error_excerpt": tail(log_path) if status == "failure" else "",
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

