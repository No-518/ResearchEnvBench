#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _git_commit(repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
    except Exception:
        return ""


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _assets_from_prepare(prepare_results: Path) -> Dict[str, Any]:
    data = _read_json(prepare_results) if prepare_results.exists() else {}
    assets = data.get("assets") if isinstance(data, dict) else None
    if isinstance(assets, dict) and "dataset" in assets and "model" in assets:
        return assets
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="build_output/cuda")
    parser.add_argument("--prepare-results", default="build_output/prepare/results.json")
    args = parser.parse_args()

    repo_root = _repo_root()
    out_dir = (repo_root / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    assets = _assets_from_prepare(repo_root / args.prepare_results)
    framework = "unknown"
    cuda_available = False
    gpu_count = 0
    detail: Dict[str, Any] = {}

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[cuda] timestamp_utc={_utc_now_iso()}\n")
        log.write(f"[cuda] python={sys.executable}\n")

        # Torch
        try:
            import torch  # type: ignore

            framework = "pytorch"
            cuda_available = bool(torch.cuda.is_available())
            gpu_count = int(torch.cuda.device_count())
            detail = {
                "torch_version": getattr(torch, "__version__", ""),
                "cuda_is_available": cuda_available,
                "device_count": gpu_count,
                "cuda_version": getattr(torch.version, "cuda", None),
            }
            log.write(f"[cuda] torch ok: {detail}\n")
        except Exception as e_torch:
            log.write(f"[cuda] torch import failed: {e_torch}\n")
            # TensorFlow
            try:
                import tensorflow as tf  # type: ignore

                framework = "tensorflow"
                gpus = tf.config.list_physical_devices("GPU")
                cuda_available = bool(gpus)
                gpu_count = len(gpus)
                detail = {"tf_version": getattr(tf, "__version__", ""), "gpus": [g.name for g in gpus]}
                log.write(f"[cuda] tensorflow ok: {detail}\n")
            except Exception as e_tf:
                log.write(f"[cuda] tensorflow import failed: {e_tf}\n")
                # JAX
                try:
                    import jax  # type: ignore

                    framework = "jax"
                    devices = jax.devices()
                    gpu_devices = [d for d in devices if d.platform == "gpu"]
                    cuda_available = bool(gpu_devices)
                    gpu_count = len(gpu_devices)
                    detail = {
                        "jax_version": getattr(jax, "__version__", ""),
                        "devices": [str(d) for d in devices],
                    }
                    log.write(f"[cuda] jax ok: {detail}\n")
                except Exception as e_jax:
                    log.write(f"[cuda] jax import failed: {e_jax}\n")
                    framework = "unknown"

    status = "success" if cuda_available else "failure"
    exit_code = 0 if cuda_available else 1
    failure_category = "unknown" if cuda_available else "runtime"
    error_excerpt = ""
    try:
        error_excerpt = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:])
    except Exception:
        pass

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": " ".join([sys.executable, "benchmark_scripts/check_cuda_available.py"] + sys.argv[1:]),
        "timeout_sec": 120,
        "framework": framework,
        "assets": assets,
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo_root),
            "env_vars": {
                "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            },
            "decision_reason": "Detect CUDA via torch/tensorflow/jax in current interpreter.",
            "timestamp_utc": _utc_now_iso(),
        },
        "observed": {
            "cuda_available": cuda_available,
            "gpu_count": gpu_count,
            "detail": detail,
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

