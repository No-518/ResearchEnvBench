#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _default_report_path() -> str:
    return os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"


def _read_report_python(report_path: str) -> Optional[str]:
    try:
        data = json.loads(Path(report_path).read_text(encoding="utf-8"))
        py = data.get("python_path")
        return py if isinstance(py, str) and py.strip() else None
    except Exception:
        return None


def _is_executable(path: str) -> bool:
    p = Path(path)
    return p.exists() and p.is_file() and os.access(str(p), os.X_OK)


def _maybe_reexec_with_report_python() -> None:
    if os.environ.get("SCIMLOPSBENCH_NO_REEXEC") == "1":
        return
    py = os.environ.get("SCIMLOPSBENCH_PYTHON") or _read_report_python(_default_report_path())
    if not py or not _is_executable(py):
        return
    try:
        if Path(py).resolve() == Path(sys.executable).resolve():
            return
    except Exception:
        pass
    os.environ["SCIMLOPSBENCH_NO_REEXEC"] = "1"
    os.execv(py, [py, str(Path(__file__).resolve())] + sys.argv[1:])


def _git_commit(repo_root: Path) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _detect_with_torch() -> Tuple[bool, int, str]:
    import torch

    available = bool(torch.cuda.is_available())
    count = int(torch.cuda.device_count()) if available else 0
    return available, count, getattr(torch, "__version__", "")


def _detect_with_tf() -> Tuple[bool, int, str]:
    import tensorflow as tf

    gpus = tf.config.list_physical_devices("GPU")
    return (len(gpus) > 0), len(gpus), getattr(tf, "__version__", "")


def _detect_with_jax() -> Tuple[bool, int, str]:
    import jax

    devices = jax.devices()
    gpu = [d for d in devices if getattr(d, "platform", "") == "gpu"]
    return (len(gpu) > 0), len(gpu), getattr(jax, "__version__", "")


def main() -> int:
    _maybe_reexec_with_report_python()

    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "cuda"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    status = "failure"
    exit_code = 1
    framework = "unknown"
    failure_category = "runtime"
    error_excerpt = ""

    observed: Dict[str, Any] = {
        "cuda_available": False,
        "gpu_count": 0,
    }

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    log(f"[cuda] python={sys.executable}")
    log(f"[cuda] argv={' '.join(sys.argv)}")

    torch_version = ""
    try:
        available, count, torch_version = _detect_with_torch()
        framework = "pytorch"
        observed["cuda_available"] = bool(available)
        observed["gpu_count"] = int(count)
        observed["torch_version"] = torch_version
        log(f"[cuda] torch cuda_available={available} gpu_count={count} torch_version={torch_version}")
        if available:
            status = "success"
            exit_code = 0
            failure_category = "unknown"
        else:
            status = "failure"
            exit_code = 1
            failure_category = "runtime"
    except Exception as e_torch:
        log(f"[cuda] torch detection failed: {e_torch}")
        try:
            available, count, tf_version = _detect_with_tf()
            framework = "tensorflow"
            observed["cuda_available"] = bool(available)
            observed["gpu_count"] = int(count)
            observed["tensorflow_version"] = tf_version
            log(f"[cuda] tf gpu_available={available} gpu_count={count} tf_version={tf_version}")
            if available:
                status = "success"
                exit_code = 0
                failure_category = "unknown"
            else:
                status = "failure"
                exit_code = 1
                failure_category = "runtime"
        except Exception as e_tf:
            log(f"[cuda] tf detection failed: {e_tf}")
            try:
                available, count, jax_version = _detect_with_jax()
                framework = "jax"
                observed["cuda_available"] = bool(available)
                observed["gpu_count"] = int(count)
                observed["jax_version"] = jax_version
                log(f"[cuda] jax gpu_available={available} gpu_count={count} jax_version={jax_version}")
                if available:
                    status = "success"
                    exit_code = 0
                    failure_category = "unknown"
                else:
                    status = "failure"
                    exit_code = 1
                    failure_category = "runtime"
            except Exception as e_jax:
                log(f"[cuda] jax detection failed: {e_jax}")
                status = "failure"
                exit_code = 1
                framework = "unknown"
                failure_category = "deps"

    error_excerpt = ""
    if status == "failure":
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            error_excerpt = "\n".join(lines[-220:])
        except Exception:
            error_excerpt = ""

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
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
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo_root),
            "env_vars": {},
            "decision_reason": "Detect CUDA availability via torch/tensorflow/jax (best-effort).",
            "timestamp_utc": _utc_timestamp(),
        },
        "observed": observed,
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
