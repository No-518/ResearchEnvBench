#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:
        return ""


def _default_assets() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _detect_cuda() -> Tuple[str, Dict[str, Any]]:
    # Prefer torch, then tensorflow, then jax.
    try:
        import torch  # type: ignore

        gpu_count = int(torch.cuda.device_count()) if torch.cuda.is_available() else 0
        devices = []
        if gpu_count > 0:
            for i in range(gpu_count):
                try:
                    devices.append(torch.cuda.get_device_name(i))
                except Exception:
                    devices.append(f"cuda:{i}")
        return "pytorch", {
            "cuda_available": bool(torch.cuda.is_available() and gpu_count > 0),
            "gpu_count": gpu_count,
            "torch_version": getattr(torch, "__version__", ""),
            "devices": devices,
        }
    except Exception as e:
        torch_err = f"{type(e).__name__}: {e}"

    try:
        import tensorflow as tf  # type: ignore

        gpus = tf.config.list_physical_devices("GPU")
        return "tensorflow", {
            "cuda_available": bool(len(gpus) > 0),
            "gpu_count": int(len(gpus)),
            "tensorflow_version": getattr(tf, "__version__", ""),
            "devices": [getattr(d, "name", "GPU") for d in gpus],
            "torch_error": torch_err,
        }
    except Exception as e:
        tf_err = f"{type(e).__name__}: {e}"

    try:
        import jax  # type: ignore

        devs = jax.devices()
        gpu_devs = [d for d in devs if getattr(d, "platform", "") == "gpu"]
        return "jax", {
            "cuda_available": bool(len(gpu_devs) > 0),
            "gpu_count": int(len(gpu_devs)),
            "jax_version": getattr(jax, "__version__", ""),
            "devices": [str(d) for d in gpu_devs],
            "torch_error": torch_err,
            "tensorflow_error": tf_err,
        }
    except Exception as e:
        jax_err = f"{type(e).__name__}: {e}"

    return "unknown", {
        "cuda_available": False,
        "gpu_count": 0,
        "torch_error": torch_err,
        "tensorflow_error": tf_err,
        "jax_error": jax_err,
    }


def main() -> int:
    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "cuda"
    _ensure_dir(stage_dir)

    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    command = f"{shlex_quote(sys.executable)} {shlex_quote(str(Path(__file__).relative_to(repo_root)))}"

    framework, observed = _detect_cuda()
    cuda_available = bool(observed.get("cuda_available", False))
    gpu_count = int(observed.get("gpu_count", 0))

    status = "success" if cuda_available else "failure"
    exit_code = 0 if cuda_available else 1

    with log_path.open("w", encoding="utf-8") as f:
        f.write(f"[cuda] python_executable={sys.executable}\n")
        f.write(f"[cuda] python_version={platform.python_version()}\n")
        f.write(f"[cuda] framework={framework}\n")
        f.write(f"[cuda] cuda_available={cuda_available}\n")
        f.write(f"[cuda] gpu_count={gpu_count}\n")
        for k, v in observed.items():
            f.write(f"[cuda] {k}={v}\n")

    error_excerpt = ""
    if not cuda_available:
        error_excerpt = "CUDA is not available according to the detected framework."

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": command,
        "timeout_sec": 120,
        "framework": framework,
        "assets": _default_assets(),
        "meta": {
            "python": sys.executable,
            "python_version": platform.python_version(),
            "git_commit": _git_commit(repo_root),
            "env_vars": {
                "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            },
            "decision_reason": "Detect CUDA availability using torch, tensorflow, or jax (in that priority).",
        },
        "observed": observed,
        "failure_category": "unknown" if cuda_available else "insufficient_hardware",
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if exit_code == 0 else 1


def shlex_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


if __name__ == "__main__":
    raise SystemExit(main())

