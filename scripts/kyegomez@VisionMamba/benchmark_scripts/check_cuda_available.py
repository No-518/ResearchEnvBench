#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main() -> int:
    root = repo_root()
    stage_dir = root / "build_output" / "cuda"
    stage_dir.mkdir(parents=True, exist_ok=True)

    results_extra = stage_dir / "results_extra.json"
    impl_py = stage_dir / "cuda_check_impl.py"

    impl_py.write_text(
        r"""
import json
import os
import sys

results_extra_path = os.environ.get("RESULTS_EXTRA_JSON", "")
framework = "unknown"
observed = {"cuda_available": False, "gpu_count": 0}
errors = {}

try:
    import torch  # noqa: F401

    framework = "pytorch"
    cuda_available = bool(torch.cuda.is_available())
    gpu_count = int(torch.cuda.device_count()) if cuda_available else 0
    observed.update(
        {
            "cuda_available": cuda_available,
            "gpu_count": gpu_count,
            "torch_version": getattr(torch, "__version__", ""),
        }
    )
    if cuda_available:
        try:
            observed["device_names"] = [torch.cuda.get_device_name(i) for i in range(gpu_count)]
        except Exception:
            pass
except Exception as e:
    errors["torch"] = str(e)
    try:
        import tensorflow as tf  # noqa: F401

        framework = "tensorflow"
        gpus = []
        try:
            gpus = tf.config.list_physical_devices("GPU")
        except Exception:
            pass
        observed.update(
            {
                "cuda_available": bool(gpus),
                "gpu_count": int(len(gpus)),
                "tensorflow_version": getattr(tf, "__version__", ""),
            }
        )
    except Exception as e2:
        errors["tensorflow"] = str(e2)
        try:
            import jax  # noqa: F401

            framework = "jax"
            devices = []
            try:
                devices = list(jax.devices())
            except Exception:
                devices = []
            gpus = [d for d in devices if getattr(d, "platform", "") == "gpu"]
            observed.update(
                {
                    "cuda_available": bool(gpus),
                    "gpu_count": int(len(gpus)),
                    "jax_version": getattr(jax, "__version__", ""),
                }
            )
        except Exception as e3:
            errors["jax"] = str(e3)

payload = {"framework": framework, "observed": observed}
if errors:
    payload["errors"] = errors

if results_extra_path:
    with open(results_extra_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

sys.exit(0 if observed.get("cuda_available") else 1)
""".lstrip(),
        encoding="utf-8",
    )

    runner = root / "benchmark_scripts" / "runner.py"
    python3 = sys.executable

    cmd = [
        python3,
        str(runner),
        "--stage",
        "cuda",
        "--task",
        "check",
        "--out-dir",
        str(stage_dir),
        "--timeout-sec",
        "120",
        "--framework",
        "unknown",
        "--requires-python",
        "--failure-category",
        "runtime",
        "--results-extra-json",
        str(results_extra),
        "--env",
        f"RESULTS_EXTRA_JSON={results_extra}",
        "--python-script",
        str(impl_py),
    ]

    proc = subprocess.run(cmd, cwd=str(root))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
