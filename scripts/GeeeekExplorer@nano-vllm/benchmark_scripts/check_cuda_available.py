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
from typing import Any


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _tail(path: Path, max_lines: int = 240) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def _resolve_report_path(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def _load_report(report_path: Path) -> dict[str, Any]:
    return json.loads(report_path.read_text(encoding="utf-8"))


def _git_commit(repo: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo), stderr=subprocess.DEVNULL, text=True)
            .strip()
        )
    except Exception:
        return ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default="")
    parser.add_argument("--python", default="")
    args = parser.parse_args(argv)

    repo = _repo_root()
    out_dir = repo / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = 120
    report_path = _resolve_report_path(args.report_path or None)
    git_commit = _git_commit(repo)

    result: dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "cuda",
        "task": "check",
        "command": "",
        "timeout_sec": timeout_sec,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": "",
            "git_commit": git_commit,
            "env_vars": {},
            "decision_reason": "Detect CUDA availability using the benchmarked Python interpreter (report.json python_path).",
            "timestamp_utc": _utc_timestamp(),
        },
        "observed": {},
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    try:
        if args.python:
            python_path = args.python
            python_source = "cli"
        else:
            report = _load_report(report_path)
            python_path = report.get("python_path", "")
            python_source = "report:python_path"

        if not isinstance(python_path, str) or not python_path.strip():
            result["failure_category"] = "missing_report"
            raise RuntimeError("python_path missing in report.json (or provide --python).")

        result["meta"]["python"] = python_path
        result["meta"]["python_source"] = python_source

        code = r"""
import json

observed = {
  "framework": "unknown",
  "cuda_available": False,
  "gpu_count": 0,
  "torch_import_ok": False,
  "torch_version": "",
  "tensorflow_import_ok": False,
  "tensorflow_version": "",
  "jax_import_ok": False,
  "jax_version": "",
}

try:
  import torch
  observed["torch_import_ok"] = True
  observed["torch_version"] = getattr(torch, "__version__", "")
  observed["framework"] = "pytorch"
  observed["cuda_available"] = bool(torch.cuda.is_available())
  try:
    observed["gpu_count"] = int(torch.cuda.device_count())
  except Exception:
    observed["gpu_count"] = 0
except Exception:
  pass

if not observed["torch_import_ok"]:
  try:
    import tensorflow as tf
    observed["tensorflow_import_ok"] = True
    observed["tensorflow_version"] = getattr(tf, "__version__", "")
    observed["framework"] = "tensorflow"
    gpus = tf.config.list_physical_devices("GPU")
    observed["gpu_count"] = len(gpus)
    observed["cuda_available"] = observed["gpu_count"] > 0
  except Exception:
    pass

if (not observed["torch_import_ok"]) and (not observed["tensorflow_import_ok"]):
  try:
    import jax
    observed["jax_import_ok"] = True
    observed["jax_version"] = getattr(jax, "__version__", "")
    observed["framework"] = "jax"
    devices = jax.devices()
    observed["gpu_count"] = sum(1 for d in devices if d.platform == "gpu")
    observed["cuda_available"] = observed["gpu_count"] > 0
  except Exception:
    pass

print(json.dumps(observed, ensure_ascii=False))
"""

        cmd = [python_path, "-c", code]
        result["command"] = " ".join(cmd[:2]) + " <inline>"

        completed = subprocess.run(cmd, cwd=str(repo), text=True, capture_output=True, timeout=timeout_sec, check=False)
        log_path.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8", errors="replace")

        observed = json.loads((completed.stdout or "{}").strip() or "{}")
        result["observed"] = observed
        result["framework"] = observed.get("framework", "unknown")

        cuda_available = bool(observed.get("cuda_available"))
        if completed.returncode != 0:
            result["failure_category"] = "runtime"
            raise RuntimeError(f"CUDA probe failed with rc={completed.returncode}")

        if cuda_available:
            result["status"] = "success"
            result["exit_code"] = 0
            result["failure_category"] = ""
            result["error_excerpt"] = ""
            results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 0

        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = "insufficient_hardware"
        result["error_excerpt"] = _tail(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    except Exception as e:
        if not log_path.exists():
            log_path.write_text("", encoding="utf-8")
        result["status"] = "failure"
        result["exit_code"] = 1
        if result.get("failure_category") in {"", "unknown"}:
            result["failure_category"] = "unknown"
        result["meta"]["exception"] = f"{type(e).__name__}: {e}"
        result["meta"]["traceback"] = traceback.format_exc(limit=60)
        result["error_excerpt"] = _tail(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

