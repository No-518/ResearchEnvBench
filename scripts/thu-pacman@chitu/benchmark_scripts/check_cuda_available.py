#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Any, Optional


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def tail_text(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_size = min(size, 512 * 1024)
            if read_size > 0:
                f.seek(-read_size, os.SEEK_END)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-max_lines:])
    except Exception:
        return ""


def git_commit(root: pathlib.Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, stderr=subprocess.STDOUT).strip()
    except Exception:
        return ""


def write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

    framework = "unknown"
    cuda_available: Optional[bool] = None
    gpu_count: Optional[int] = None
    failure_category = "unknown"
    exit_code = 1

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[cuda] python={sys.executable}\n")
        log.write(f"[cuda] argv={sys.argv}\n")

    try:
        import torch  # type: ignore

        framework = "pytorch"
        cuda_available = bool(torch.cuda.is_available())
        gpu_count = int(torch.cuda.device_count())
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[cuda] torch.__version__={getattr(torch,'__version__',None)}\n")
            log.write(f"[cuda] torch.cuda.is_available()={cuda_available}\n")
            log.write(f"[cuda] torch.cuda.device_count()={gpu_count}\n")
        if cuda_available and gpu_count > 0:
            exit_code = 0
            failure_category = "unknown"
        else:
            exit_code = 1
            failure_category = "runtime"

    except Exception as e_torch:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[cuda] torch import failed: {e_torch}\n")
        # Try tensorflow then jax (best-effort).
        try:
            import tensorflow as tf  # type: ignore

            framework = "tensorflow"
            gpus = tf.config.list_physical_devices("GPU")
            gpu_count = len(gpus)
            cuda_available = gpu_count > 0
            exit_code = 0 if cuda_available else 1
            failure_category = "runtime" if exit_code else "unknown"
        except Exception as e_tf:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"[cuda] tensorflow import failed: {e_tf}\n")
            try:
                import jax  # type: ignore

                framework = "jax"
                devices = list(jax.devices())
                gpu_count = sum(1 for d in devices if d.platform == "gpu")
                cuda_available = gpu_count > 0
                exit_code = 0 if cuda_available else 1
                failure_category = "runtime" if exit_code else "unknown"
            except Exception as e_jax:
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"[cuda] jax import failed: {e_jax}\n")
                framework = "unknown"
                cuda_available = None
                gpu_count = None
                exit_code = 1
                failure_category = "deps"

    status = "success" if exit_code == 0 else "failure"
    results = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": f"{sys.executable} {pathlib.Path(__file__).name}",
        "timeout_sec": 120,
        "framework": framework,
        "assets": assets,
        "meta": {
            "python": sys.executable,
            "git_commit": git_commit(root),
            "env_vars": {
                "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            },
            "decision_reason": "Probe CUDA availability via available ML framework imports.",
        },
        "observed": {
            "cuda_available": cuda_available,
            "gpu_count": gpu_count,
        },
        "failure_category": failure_category,
        "error_excerpt": tail_text(log_path),
    }
    write_json(results_path, results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

