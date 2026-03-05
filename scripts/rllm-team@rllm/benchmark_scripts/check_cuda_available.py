#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


def tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def detect_cuda() -> Tuple[str, Dict[str, Any]]:
    # Returns (framework, observed)
    observed: Dict[str, Any] = {}

    try:
        import torch  # type: ignore

        framework = "pytorch"
        cuda_available = bool(torch.cuda.is_available())
        gpu_count = int(torch.cuda.device_count()) if cuda_available else int(torch.cuda.device_count())
        observed.update(
            {
                "cuda_available": cuda_available,
                "gpu_count": gpu_count,
                "torch_version": getattr(torch, "__version__", ""),
            }
        )
        if cuda_available and gpu_count > 0:
            try:
                names = []
                for i in range(gpu_count):
                    names.append(torch.cuda.get_device_name(i))
                observed["gpu_names"] = names
            except Exception:
                pass
        return framework, observed
    except Exception as e:
        observed["torch_import_error"] = str(e)

    try:
        import tensorflow as tf  # type: ignore

        framework = "tensorflow"
        gpus = tf.config.list_physical_devices("GPU")
        observed.update(
            {
                "cuda_available": bool(gpus),
                "gpu_count": len(gpus),
                "tensorflow_version": getattr(tf, "__version__", ""),
                "gpu_devices": [d.name for d in gpus],
            }
        )
        return framework, observed
    except Exception as e:
        observed["tensorflow_import_error"] = str(e)

    try:
        import jax  # type: ignore

        framework = "jax"
        try:
            gpus = jax.devices("gpu")
        except Exception:
            gpus = []
        observed.update(
            {
                "cuda_available": bool(gpus),
                "gpu_count": len(gpus),
                "jax_version": getattr(jax, "__version__", ""),
                "gpu_devices": [str(d) for d in gpus],
            }
        )
        return framework, observed
    except Exception as e:
        observed["jax_import_error"] = str(e)

    observed.update({"cuda_available": False, "gpu_count": 0})
    return "unknown", observed


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = 120
    command = " ".join(
        shlex.quote(x)
        for x in [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    )

    status = "failure"
    skip_reason = "not_applicable"
    exit_code = 1
    failure_category = "unknown"
    observed: Dict[str, Any] = {}
    framework = "unknown"

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"[cuda] timestamp_utc={utc_timestamp()}\n")
        logf.write(f"[cuda] command={command}\n")
        logf.write(f"[cuda] sys.executable={sys.executable}\n")
        logf.write(f"[cuda] cwd={os.getcwd()}\n")
        try:
            framework, observed = detect_cuda()
            logf.write(f"[cuda] framework={framework}\n")
            logf.write(f"[cuda] observed={json.dumps(observed, ensure_ascii=False)}\n")
            if bool(observed.get("cuda_available")):
                status = "success"
                exit_code = 0
                failure_category = "unknown"
            else:
                status = "failure"
                exit_code = 1
                failure_category = "unknown"
        except Exception:
            failure_category = "runtime"
            observed["exception"] = traceback.format_exc()
            logf.write("[cuda] exception:\n")
            logf.write(observed["exception"])

    results: Dict[str, Any] = {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": command,
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "observed": observed,
        "meta": {
            "python": sys.executable,
            "git_commit": git_commit(root),
            "env_vars": {
                k: os.environ.get(k, "")
                for k in [
                    "CUDA_VISIBLE_DEVICES",
                    "HF_HOME",
                    "TRANSFORMERS_CACHE",
                    "HF_DATASETS_CACHE",
                    "PIP_CACHE_DIR",
                    "XDG_CACHE_HOME",
                    "SENTENCE_TRANSFORMERS_HOME",
                    "TORCH_HOME",
                    "PYTHONDONTWRITEBYTECODE",
                    "SCIMLOPSBENCH_REPORT",
                    "SCIMLOPSBENCH_PYTHON",
                ]
            },
            "decision_reason": "Detect CUDA availability via torch/tensorflow/jax in that order.",
            "timestamp_utc": utc_timestamp(),
        },
        "failure_category": failure_category,
        "error_excerpt": tail_text(log_path),
    }

    results_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

