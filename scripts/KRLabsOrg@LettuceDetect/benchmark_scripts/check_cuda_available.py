#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path("/opt/scimlopsbench/report.json")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def _maybe_reexec_under_report_python(report_path: Path) -> tuple[bool, str]:
    """Return (did_reexec, failure_category)."""
    if os.environ.get("_SCIMLOPSBENCH_REEXEC") == "1":
        return False, ""

    cli_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if cli_python and _is_executable_file(Path(cli_python)):
        if os.path.realpath(sys.executable) != os.path.realpath(cli_python):
            os.environ["_SCIMLOPSBENCH_REEXEC"] = "1"
            os.execv(cli_python, [cli_python, *sys.argv])
        return False, ""

    if not report_path.exists():
        return False, "missing_report"
    try:
        report = _read_json(report_path)
    except Exception:
        return False, "missing_report"

    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        return False, "missing_report"

    python_path = python_path.strip()
    if not _is_executable_file(Path(python_path)):
        return False, "missing_report"

    if os.path.realpath(sys.executable) != os.path.realpath(python_path):
        os.environ["_SCIMLOPSBENCH_REEXEC"] = "1"
        os.execv(python_path, [python_path, *sys.argv])
    return False, ""


def _git_commit(repo: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            check=False,
            capture_output=True,
            text=True,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def _tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    return "\n".join(lines[-max_lines:])


def main() -> int:
    parser = argparse.ArgumentParser(description="Check CUDA availability via installed ML frameworks")
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    repo = _repo_root()
    out_dir = repo / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"timestamp_utc={_utc_timestamp()}\n")
        logf.write(f"sys.executable={sys.executable}\n")
        logf.write(f"report_path={report_path}\n")

    _, precheck_failure = _maybe_reexec_under_report_python(report_path)
    if precheck_failure:
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "cuda",
            "task": "check",
            "command": "python benchmark_scripts/check_cuda_available.py",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {
                "dataset": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
                "model": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
            },
            "meta": {
                "python": sys.executable,
                "git_commit": _git_commit(repo),
                "env_vars": {k: v for k, v in os.environ.items() if k in {"CUDA_VISIBLE_DEVICES"}},
                "decision_reason": "Global benchmark policy requires using python_path from agent report.json for CUDA check.",
                "report_path": str(report_path),
            },
            "observed": {},
            "failure_category": precheck_failure,
            "error_excerpt": _tail_text(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    framework = "unknown"
    observed: dict[str, Any] = {}
    cuda_available = False

    # Torch
    try:
        import torch  # type: ignore

        framework = "pytorch"
        torch_cuda = bool(torch.cuda.is_available())
        torch_gpu_count = int(torch.cuda.device_count()) if torch_cuda else 0
        observed.update(
            {
                "torch_import_ok": True,
                "torch_version": getattr(torch, "__version__", ""),
                "cuda_available": torch_cuda and torch_gpu_count > 0,
                "gpu_count": torch_gpu_count,
            }
        )
        cuda_available = bool(observed["cuda_available"])
    except Exception as e:
        observed.update({"torch_import_ok": False, "torch_error": repr(e)})

    # TensorFlow
    if framework == "unknown" and not cuda_available:
        try:
            import tensorflow as tf  # type: ignore

            framework = "tensorflow"
            gpus = tf.config.list_physical_devices("GPU")
            observed.update(
                {
                    "tf_import_ok": True,
                    "tf_version": getattr(tf, "__version__", ""),
                    "cuda_available": bool(gpus),
                    "gpu_count": int(len(gpus)),
                }
            )
            cuda_available = bool(observed["cuda_available"])
        except Exception as e:
            observed.update({"tf_import_ok": False, "tf_error": repr(e)})

    # JAX
    if framework == "unknown" and not cuda_available:
        try:
            import jax  # type: ignore

            framework = "jax"
            devices = list(jax.devices())
            gpu_devices = [d for d in devices if getattr(d, "platform", "") == "gpu"]
            observed.update(
                {
                    "jax_import_ok": True,
                    "jax_version": getattr(jax, "__version__", ""),
                    "cuda_available": bool(gpu_devices),
                    "gpu_count": int(len(gpu_devices)),
                }
            )
            cuda_available = bool(observed["cuda_available"])
        except Exception as e:
            observed.update({"jax_import_ok": False, "jax_error": repr(e)})

    status = "success" if cuda_available else "failure"
    exit_code = 0 if cuda_available else 1

    payload = {
        "status": status,
        "skip_reason": "unknown" if cuda_available else "insufficient_hardware",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": "python benchmark_scripts/check_cuda_available.py",
        "timeout_sec": 120,
        "framework": framework,
        "assets": {
            "dataset": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
            "model": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        },
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo),
            "env_vars": {k: v for k, v in os.environ.items() if k in {"CUDA_VISIBLE_DEVICES"}},
            "decision_reason": "Check CUDA availability using the ML framework available in the benchmark python environment.",
            "report_path": str(report_path),
        },
        "observed": observed,
        "failure_category": "unknown" if cuda_available else "unknown",
        "error_excerpt": _tail_text(log_path),
    }

    with log_path.open("a", encoding="utf-8") as logf:
        logf.write("\n--- observed ---\n")
        logf.write(json.dumps(observed, ensure_ascii=False, indent=2) + "\n")

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

