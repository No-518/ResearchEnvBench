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


def git_commit(root: Path) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def tail_lines(path: Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:]) if len(lines) > n else "\n".join(lines)
    except Exception:
        return ""


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def detect_cuda() -> Tuple[str, bool, int, Dict[str, Any]]:
    details: Dict[str, Any] = {}

    try:
        import torch  # type: ignore

        cuda_ok = bool(torch.cuda.is_available())
        gpu_count = int(torch.cuda.device_count()) if hasattr(torch.cuda, "device_count") else 0
        details["torch_version"] = getattr(torch, "__version__", "")
        details["torch_cuda_version"] = getattr(torch.version, "cuda", None)
        return "pytorch", cuda_ok, gpu_count, details
    except Exception as e:
        details["torch_import_error"] = repr(e)

    try:
        import tensorflow as tf  # type: ignore

        gpus = tf.config.list_physical_devices("GPU")
        cuda_ok = bool(gpus)
        gpu_count = len(gpus)
        details["tensorflow_version"] = getattr(tf, "__version__", "")
        details["tf_gpus"] = [getattr(d, "name", str(d)) for d in gpus]
        return "tensorflow", cuda_ok, gpu_count, details
    except Exception as e:
        details["tensorflow_import_error"] = repr(e)

    try:
        import jax  # type: ignore

        gpus = [d for d in jax.devices() if getattr(d, "platform", "") == "gpu"]
        cuda_ok = bool(gpus)
        gpu_count = len(gpus)
        details["jax_version"] = getattr(jax, "__version__", "")
        details["jax_devices"] = [str(d) for d in jax.devices()]
        return "jax", cuda_ok, gpu_count, details
    except Exception as e:
        details["jax_import_error"] = repr(e)

    return "unknown", False, 0, details


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    started = time.time()
    with log_path.open("w", encoding="utf-8") as log:
        log.write("[cuda] checking CUDA availability\n")
        log.write(f"[cuda] sys.executable={sys.executable}\n")
        log.write(f"[cuda] cwd={os.getcwd()}\n")
        log.flush()

        framework, cuda_ok, gpu_count, details = detect_cuda()
        log.write(f"[cuda] framework={framework}\n")
        log.write(f"[cuda] cuda_available={cuda_ok}\n")
        log.write(f"[cuda] gpu_count={gpu_count}\n")
        log.write(f"[cuda] details={json.dumps(details, ensure_ascii=False)}\n")

    status = "success" if cuda_ok else "failure"
    exit_code = 0 if cuda_ok else 1

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": f"{sys.executable} {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": framework if framework in ("pytorch", "tensorflow", "jax") else "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": sys.version.split()[0],
            "git_commit": git_commit(root),
            "env_vars": {k: os.environ.get(k, "") for k in ["CUDA_VISIBLE_DEVICES"] if k in os.environ},
            "decision_reason": "Probe common ML frameworks (torch/tensorflow/jax) to determine CUDA availability.",
            "duration_sec": round(time.time() - started, 3),
        },
        "observed": {
            "cuda_available": cuda_ok,
            "gpu_count": gpu_count,
            **details,
        },
        "failure_category": "" if cuda_ok else "unknown",
        "error_excerpt": "" if cuda_ok else tail_lines(log_path),
    }
    tmp = results_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(results_path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

