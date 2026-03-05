#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def git_commit(root: Path) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if p.returncode == 0:
            return p.stdout.strip()
    except Exception:
        return ""
    return ""


def safe_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def detect_with_torch() -> Tuple[bool, int, Optional[str]]:
    try:
        import torch  # type: ignore

        return bool(torch.cuda.is_available()), int(torch.cuda.device_count()), None
    except Exception as e:
        return False, 0, f"torch import/check failed: {e}"


def detect_with_tensorflow() -> Tuple[bool, int, Optional[str]]:
    try:
        import tensorflow as tf  # type: ignore

        gpus = tf.config.list_physical_devices("GPU")
        return bool(gpus), int(len(gpus)), None
    except Exception as e:
        return False, 0, f"tensorflow import/check failed: {e}"


def detect_with_jax() -> Tuple[bool, int, Optional[str]]:
    try:
        import jax  # type: ignore

        devices = jax.devices()
        gpu_devices = [d for d in devices if getattr(d, "platform", "") == "gpu"]
        return bool(gpu_devices), int(len(gpu_devices)), None
    except Exception as e:
        return False, 0, f"jax import/check failed: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=None, help="Default: build_output/cuda")
    args = ap.parse_args()

    root = repo_root()
    out_dir = Path(args.out_dir) if args.out_dir else (root / "build_output" / "cuda")
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    started = datetime.now(tz=timezone.utc)
    command_str = f"{sys.executable} benchmark_scripts/check_cuda_available.py"

    observed: Dict[str, Any] = {}
    status = "failure"
    exit_code = 1
    failure_category = "unknown"
    framework = "unknown"

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write("== cuda check stage ==\n")
        log_f.write(f"command: {command_str}\n")
        log_f.write(f"python: {sys.executable}\n")
        log_f.write(f"timestamp_utc: {started.isoformat()}\n")

        try:
            cuda_ok, gpu_count, err = detect_with_torch()
            if err is None:
                framework = "pytorch"
                observed.update({"cuda_available": cuda_ok, "gpu_count": gpu_count, "backend": "torch"})
            else:
                log_f.write(err + "\n")
                tf_ok, tf_count, tf_err = detect_with_tensorflow()
                if tf_err is None:
                    framework = "tensorflow"
                    observed.update({"cuda_available": tf_ok, "gpu_count": tf_count, "backend": "tensorflow"})
                else:
                    log_f.write(tf_err + "\n")
                    jax_ok, jax_count, jax_err = detect_with_jax()
                    if jax_err is None:
                        framework = "jax"
                        observed.update({"cuda_available": jax_ok, "gpu_count": jax_count, "backend": "jax"})
                    else:
                        log_f.write(jax_err + "\n")
                        observed.update({"cuda_available": False, "gpu_count": 0, "backend": "none"})

            if bool(observed.get("cuda_available")):
                status = "success"
                exit_code = 0
                failure_category = "unknown"
            else:
                status = "failure"
                exit_code = 1
                failure_category = "insufficient_hardware"
        except Exception:
            log_f.write("exception\n")
            log_f.write(traceback.format_exc() + "\n")
            status = "failure"
            exit_code = 1
            failure_category = "runtime"
            observed.update({"cuda_available": False, "gpu_count": 0, "backend": "error"})

    ended = datetime.now(tz=timezone.utc)
    error_excerpt = ""
    try:
        error_excerpt = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:])
    except Exception:
        error_excerpt = ""

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": int(exit_code),
        "stage": "cuda",
        "task": "check",
        "command": command_str,
        "timeout_sec": 60,
        "framework": framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": sys.executable,
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "git_commit": git_commit(root),
            "env_vars": {k: os.environ.get(k, "") for k in ["CUDA_VISIBLE_DEVICES"] if k in os.environ},
            "decision_reason": "Detect CUDA availability via torch/tensorflow/jax in the benchmark python environment.",
            "start_time_utc": started.isoformat(),
            "end_time_utc": ended.isoformat(),
            "duration_sec": max(0.0, (ended - started).total_seconds()),
        },
        "observed": observed,
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    safe_write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

