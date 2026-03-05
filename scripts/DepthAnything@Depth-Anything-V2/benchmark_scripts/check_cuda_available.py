#!/usr/bin/env python3
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def tail(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-max_lines:])


def try_git_commit(root: Path) -> str:
    try:
        import subprocess

        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def detect_cuda() -> Tuple[str, bool, int, Dict[str, Any], Optional[str]]:
    # Returns: (framework, cuda_available, gpu_count, details, error)
    try:
        import torch  # type: ignore

        try:
            cuda_available = bool(torch.cuda.is_available())
            gpu_count = int(torch.cuda.device_count())
            details = {
                "torch_version": getattr(torch, "__version__", ""),
                "device_count": gpu_count,
            }
            if cuda_available and gpu_count > 0:
                names = []
                for i in range(min(gpu_count, 4)):
                    try:
                        names.append(torch.cuda.get_device_name(i))
                    except Exception:
                        names.append("")
                details["device_names_sample"] = names
            return "pytorch", cuda_available, gpu_count, details, None
        except Exception as e:
            return "pytorch", False, 0, {"torch_version": getattr(torch, "__version__", "")}, f"torch_cuda_check_failed: {e}"
    except Exception:
        pass

    try:
        import tensorflow as tf  # type: ignore

        gpus = tf.config.list_physical_devices("GPU")
        gpu_count = len(gpus)
        return (
            "tensorflow",
            gpu_count > 0,
            gpu_count,
            {"tf_version": getattr(tf, "__version__", ""), "gpus": [d.name for d in gpus[:4]]},
            None,
        )
    except Exception:
        pass

    try:
        import jax  # type: ignore

        devices = jax.devices()
        gpu_devices = [d for d in devices if getattr(d, "platform", "") == "gpu"]
        return (
            "jax",
            len(gpu_devices) > 0,
            len(gpu_devices),
            {"jax_version": getattr(jax, "__version__", ""), "devices_sample": [str(d) for d in gpu_devices[:4]]},
            None,
        )
    except Exception:
        pass

    return "unknown", False, 0, {}, "no_supported_framework_found (torch/tensorflow/jax all unavailable)"


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    framework, cuda_available, gpu_count, details, err = detect_cuda()

    with log_path.open("w", encoding="utf-8") as f:
        f.write(f"[cuda] timestamp_utc={utc_now_iso()}\n")
        f.write(f"[cuda] framework={framework}\n")
        f.write(f"[cuda] cuda_available={cuda_available}\n")
        f.write(f"[cuda] gpu_count={gpu_count}\n")
        if details:
            f.write(f"[cuda] details={json.dumps(details, ensure_ascii=False)}\n")
        if err:
            f.write(f"[cuda] error={err}\n")

    status = "success"
    exit_code = 0
    skip_reason = "unknown"
    failure_category = ""

    if err and framework == "unknown":
        status = "failure"
        exit_code = 1
        failure_category = "deps"
    elif not cuda_available:
        status = "failure"
        exit_code = 1
        skip_reason = "insufficient_hardware"
        failure_category = "unknown"

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": f"{sys.executable} benchmark_scripts/check_cuda_available.py",
        "timeout_sec": 120,
        "framework": framework if framework in {"pytorch", "tensorflow", "jax"} else "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": f"{sys.executable} ({platform.python_version()})",
            "git_commit": try_git_commit(root),
            "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "")},
            "decision_reason": "Detect CUDA availability via torch/tensorflow/jax import and device enumeration.",
            "timestamp_utc": utc_now_iso(),
        },
        "observed": {
            "cuda_available": cuda_available,
            "gpu_count": gpu_count,
            "details": details,
            "error": err or "",
        },
        "failure_category": failure_category,
        "error_excerpt": tail(log_path),
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

