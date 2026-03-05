#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _collect_env_vars() -> Dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "NVIDIA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
    ]
    return {k: os.environ.get(k, "") for k in keys if k in os.environ}


def _detect_cuda() -> Tuple[str, bool, int, Dict[str, Any], str]:
    # returns: (framework, cuda_available, gpu_count, details, error)
    try:
        import torch  # type: ignore

        cuda_ok = bool(getattr(torch, "cuda", None) and torch.cuda.is_available())
        gpu_count = int(torch.cuda.device_count()) if cuda_ok else 0
        details = {"torch_version": getattr(torch, "__version__", ""), "torch_cuda_version": getattr(torch.version, "cuda", "")}
        return "pytorch", cuda_ok, gpu_count, details, ""
    except Exception as e:
        torch_err = repr(e)

    try:
        import tensorflow as tf  # type: ignore

        gpus = tf.config.list_physical_devices("GPU")
        cuda_ok = len(gpus) > 0
        details = {"tensorflow_version": getattr(tf, "__version__", ""), "gpus": [getattr(g, "name", str(g)) for g in gpus]}
        return "tensorflow", cuda_ok, len(gpus), details, ""
    except Exception as e:
        tf_err = repr(e)

    try:
        import jax  # type: ignore

        gpus = [d for d in jax.devices() if getattr(d, "platform", "") == "gpu"]
        cuda_ok = len(gpus) > 0
        details = {"jax_version": getattr(jax, "__version__", ""), "devices": [str(d) for d in jax.devices()]}
        return "jax", cuda_ok, len(gpus), details, ""
    except Exception as e:
        jax_err = repr(e)

    return "unknown", False, 0, {"torch_error": torch_err, "tensorflow_error": tf_err, "jax_error": jax_err}, "no_supported_framework"


def main() -> int:
    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(line: str) -> None:
        msg = f"[{_utc_now_iso()}] {line}"
        print(msg)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    # fresh log
    log_path.write_text("", encoding="utf-8")

    framework, cuda_ok, gpu_count, details, err = _detect_cuda()
    log(f"framework={framework} cuda_available={cuda_ok} gpu_count={gpu_count}")
    if details:
        log("details=" + json.dumps(details, ensure_ascii=False))
    if err:
        log(f"error={err}")

    status = "success" if cuda_ok else "failure"
    exit_code = 0 if cuda_ok else 1
    failure_category = "not_applicable" if cuda_ok else "runtime"
    skip_reason = "not_applicable" if cuda_ok else "insufficient_hardware"

    results = {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": f"{sys.executable} benchmark_scripts/check_cuda_available.py",
        "timeout_sec": 120,
        "framework": framework if framework in ("pytorch", "tensorflow", "jax") else "unknown",
        "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
        "meta": {
            "python": f"{sys.executable} ({platform.python_version()})",
            "git_commit": _git_commit(repo_root),
            "env_vars": _collect_env_vars(),
            "decision_reason": "Detect CUDA via torch/tensorflow/jax in the active environment.",
            "observed": {"cuda_available": cuda_ok, "gpu_count": gpu_count, **details},
        },
        "failure_category": failure_category,
        "error_excerpt": (log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:] if exit_code == 1 else []),
    }

    # Normalize error_excerpt to string
    if isinstance(results["error_excerpt"], list):
        results["error_excerpt"] = "\n".join(results["error_excerpt"])

    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

