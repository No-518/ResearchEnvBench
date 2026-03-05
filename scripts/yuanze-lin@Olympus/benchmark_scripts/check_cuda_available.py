#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json: {path}: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"failed reading json: {path}: {e}"


def tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, timeout=5)
            .strip()
        )
    except Exception:
        return ""


def load_assets(root: Path) -> Dict[str, Dict[str, str]]:
    manifest = root / "benchmark_assets" / "manifest.json"
    data, _ = safe_read_json(manifest)
    if not isinstance(data, dict):
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    ds = data.get("dataset") if isinstance(data.get("dataset"), dict) else {}
    md = data.get("model") if isinstance(data.get("model"), dict) else {}
    return {
        "dataset": {
            "path": str(ds.get("path", "")),
            "source": str(ds.get("source", "")),
            "version": str(ds.get("version", "")),
            "sha256": str(ds.get("sha256", "")),
        },
        "model": {
            "path": str(md.get("path", "")),
            "source": str(md.get("source", "")),
            "version": str(md.get("version", "")),
            "sha256": str(md.get("sha256", "")),
        },
    }


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(msg: str) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")

    log_path.write_text("", encoding="utf-8")
    log(f"[cuda] timestamp_utc={utc_ts()}")
    log(f"[cuda] python={sys.executable}")
    log(f"[cuda] python_version={platform.python_version()}")
    log(f"[cuda] CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES', '')}")

    assets = load_assets(root)
    observed: Dict[str, Any] = {"cuda_available": False, "gpu_count": 0}

    framework = "unknown"
    status = "failure"
    failure_category = "unknown"
    exit_code = 1
    error = ""

    try:
        try:
            import torch  # type: ignore

            framework = "pytorch"
            try:
                is_avail = bool(torch.cuda.is_available())
                count = int(torch.cuda.device_count())
                observed["cuda_available"] = bool(is_avail and count > 0)
                observed["gpu_count"] = count
                if count > 0:
                    try:
                        observed["gpu_0_name"] = torch.cuda.get_device_name(0)
                    except Exception:
                        pass
                log(f"[cuda] torch.__version__={getattr(torch, '__version__', '')}")
                log(f"[cuda] torch.cuda.is_available()={is_avail}")
                log(f"[cuda] torch.cuda.device_count()={count}")
            except Exception as e:  # noqa: BLE001
                error = f"torch cuda check failed: {e}"
                failure_category = "runtime"
        except Exception:
            try:
                import tensorflow as tf  # type: ignore

                framework = "tensorflow"
                gpus = tf.config.list_physical_devices("GPU")
                observed["gpu_count"] = len(gpus)
                observed["cuda_available"] = len(gpus) > 0
                log(f"[cuda] tensorflow.__version__={getattr(tf, '__version__', '')}")
                log(f"[cuda] tf GPUs={len(gpus)}")
            except Exception:
                try:
                    import jax  # type: ignore

                    framework = "jax"
                    devices = getattr(jax, "devices", lambda: [])()
                    gpu_devs = [d for d in devices if getattr(d, "platform", "") == "gpu"]
                    observed["gpu_count"] = len(gpu_devs)
                    observed["cuda_available"] = len(gpu_devs) > 0
                    log(f"[cuda] jax backend={getattr(jax, 'default_backend', lambda: '')()}")
                    log(f"[cuda] jax gpu devices={len(gpu_devs)}")
                except Exception as e:
                    error = f"No supported framework importable (torch/tensorflow/jax): {e}"
                    failure_category = "deps"

        if observed.get("cuda_available"):
            status = "success"
            exit_code = 0
        else:
            status = "failure"
            exit_code = 1
            if failure_category == "unknown":
                failure_category = "runtime"
    except Exception as e:  # noqa: BLE001
        status = "failure"
        exit_code = 1
        failure_category = "unknown"
        error = str(e)

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": f"{sys.executable} benchmark_scripts/check_cuda_available.py",
        "timeout_sec": 120,
        "framework": framework,
        "assets": {"dataset": assets["dataset"], "model": assets["model"]},
        "observed": observed,
        "meta": {
            "python": sys.executable,
            "python_version": platform.python_version(),
            "git_commit": git_commit(root),
            "env_vars": {
                "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            },
            "timestamp_utc": utc_ts(),
        },
        "failure_category": failure_category,
        "error_excerpt": tail_lines(log_path) if status == "failure" else "",
    }
    if error:
        payload["meta"]["error"] = error

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
