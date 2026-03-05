#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_text(path: Path, content: str) -> None:
    _safe_mkdir(path.parent)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    _safe_mkdir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tail(path: Path, max_lines: int = 240) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        return "".join(lines[-max_lines:])
    except Exception:
        return ""


def _git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


def _check_torch() -> Tuple[bool, int, Optional[str]]:
    try:
        import torch  # type: ignore

        available = bool(torch.cuda.is_available())
        count = int(torch.cuda.device_count()) if available else int(torch.cuda.device_count())
        return available, count, getattr(torch, "__version__", None)
    except Exception:
        return False, 0, None


def _check_tensorflow() -> Tuple[bool, int, Optional[str]]:
    try:
        import tensorflow as tf  # type: ignore

        gpus = tf.config.list_physical_devices("GPU")
        available = bool(gpus)
        return available, len(gpus), getattr(tf, "__version__", None)
    except Exception:
        return False, 0, None


def _check_jax() -> Tuple[bool, int, Optional[str]]:
    try:
        import jax  # type: ignore

        devices = jax.devices()
        gpu_devices = [d for d in devices if getattr(d, "platform", "").lower() == "gpu"]
        available = bool(gpu_devices)
        return available, len(gpu_devices), getattr(jax, "__version__", None)
    except Exception:
        return False, 0, None


def _count_nvidia_smi() -> int:
    try:
        if not shutil.which("nvidia-smi"):
            return 0
        out = subprocess.check_output(["nvidia-smi", "-L"], text=True, stderr=subprocess.DEVNULL)
        lines = [ln for ln in out.splitlines() if ln.strip().startswith("GPU")]
        return len(lines)
    except Exception:
        return 0


def _check_onnxruntime() -> Tuple[bool, Optional[str], list[str]]:
    try:
        import onnxruntime as ort  # type: ignore

        providers = []
        try:
            providers = list(ort.get_available_providers())
        except Exception:
            providers = []
        available = ("CUDAExecutionProvider" in providers) or (str(getattr(ort, "get_device", lambda: "")()).upper() == "GPU")
        return bool(available), getattr(ort, "__version__", None), providers
    except Exception:
        return False, None, []


def main() -> int:
    out_dir = REPO_ROOT / "build_output" / "cuda"
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    _safe_mkdir(out_dir)

    header = (
        f"stage=cuda\n"
        f"repo={REPO_ROOT}\n"
        f"out_dir={out_dir}\n"
        f"timestamp_utc={_utc_timestamp()}\n"
        f"python={sys.executable}\n"
    )
    _write_text(log_path, header)

    result: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "cuda",
        "task": "check",
        "command": f"{sys.executable} benchmark_scripts/check_cuda_available.py",
        "timeout_sec": 600,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(),
            "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "")},
            "decision_reason": "Detect CUDA availability via torch/tensorflow/jax if installed.",
            "timestamp_utc": _utc_timestamp(),
        },
        "observed": {
            "framework": "unknown",
            "cuda_available": False,
            "gpu_count": 0,
            "versions": {},
            "onnxruntime_providers": [],
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    try:
        # Baseline GPU count even if no ML framework is installed.
        smi_count = _count_nvidia_smi()
        if smi_count:
            result["observed"]["gpu_count"] = int(smi_count)

        torch_avail, torch_count, torch_ver = _check_torch()
        if torch_ver:
            result["observed"]["versions"]["torch"] = torch_ver
        if torch_avail or torch_ver is not None:
            result["observed"]["framework"] = "pytorch"
            result["framework"] = "pytorch"
            result["observed"]["cuda_available"] = bool(torch_avail)
            result["observed"]["gpu_count"] = int(torch_count)
        else:
            tf_avail, tf_count, tf_ver = _check_tensorflow()
            if tf_ver:
                result["observed"]["versions"]["tensorflow"] = tf_ver
            if tf_avail or tf_ver is not None:
                result["observed"]["framework"] = "tensorflow"
                result["framework"] = "tensorflow"
                result["observed"]["cuda_available"] = bool(tf_avail)
                result["observed"]["gpu_count"] = int(tf_count)
            else:
                jax_avail, jax_count, jax_ver = _check_jax()
                if jax_ver:
                    result["observed"]["versions"]["jax"] = jax_ver
                if jax_avail or jax_ver is not None:
                    result["observed"]["framework"] = "jax"
                    result["framework"] = "jax"
                    result["observed"]["cuda_available"] = bool(jax_avail)
                    result["observed"]["gpu_count"] = int(jax_count)
                else:
                    # Repo uses ONNXRuntime by default; detect CUDA provider even if torch/tf/jax aren't installed.
                    ort_avail, ort_ver, ort_providers = _check_onnxruntime()
                    if ort_ver:
                        result["observed"]["versions"]["onnxruntime"] = ort_ver
                    result["observed"]["onnxruntime_providers"] = ort_providers
                    if ort_avail:
                        result["observed"]["framework"] = "onnxruntime"
                        result["framework"] = "unknown"
                        result["observed"]["cuda_available"] = True
                        # Keep any nvidia-smi gpu_count; if missing, leave as-is.

        if result["observed"]["cuda_available"]:
            result["status"] = "success"
            result["exit_code"] = 0
            result["failure_category"] = "unknown"
            _write_json(results_path, result)
            return 0

        # CUDA not available -> failure (as required)
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = "runtime"
        result["error_excerpt"] = _tail(log_path, 200)
        _write_json(results_path, result)
        return 1
    except Exception as e:
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
            f.write("\nERROR:\n")
            f.write(f"{type(e).__name__}: {e}\n")
            f.write(traceback.format_exc() + "\n")
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = "unknown"
        result["error_excerpt"] = _tail(log_path, 240)
        _write_json(results_path, result)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
