#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail_text(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def load_report(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not report_path.exists():
        return None, "missing_report"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def git_commit(root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT, text=True)
        return out.strip()
    except Exception:
        return ""


def run_python_check(python_path: str) -> Tuple[int, str, str]:
    code = r"""
import json
import sys

out = {
  "framework": "unknown",
  "cuda_available": False,
  "gpu_count": 0,
  "python_version": sys.version.split()[0],
  "torch_import_ok": False,
  "torch_version": "",
}

def done(exit_code: int):
  print(json.dumps(out))
  sys.exit(exit_code)

try:
  import torch
  out["framework"] = "pytorch"
  out["torch_import_ok"] = True
  out["torch_version"] = getattr(torch, "__version__", "")
  out["cuda_available"] = bool(torch.cuda.is_available())
  out["gpu_count"] = int(torch.cuda.device_count())
  done(0 if out["cuda_available"] else 1)
except Exception as e:
  out["torch_import_error"] = str(e)

try:
  import tensorflow as tf
  out["framework"] = "tensorflow"
  gpus = tf.config.list_physical_devices("GPU")
  out["cuda_available"] = bool(gpus)
  out["gpu_count"] = int(len(gpus))
  done(0 if out["cuda_available"] else 1)
except Exception as e:
  out["tensorflow_import_error"] = str(e)

try:
  import jax
  out["framework"] = "jax"
  devs = jax.devices()
  gpu_devs = [d for d in devs if getattr(d, "platform", "") == "gpu"]
  out["cuda_available"] = bool(gpu_devs)
  out["gpu_count"] = int(len(gpu_devs))
  done(0 if out["cuda_available"] else 1)
except Exception as e:
  out["jax_import_error"] = str(e)

done(1)
"""
    try:
        completed = subprocess.run(
            [python_path, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1"),
        )
        return int(completed.returncode), completed.stdout, completed.stderr
    except Exception as e:
        return 1, "", str(e)


def main() -> int:
    ap = argparse.ArgumentParser(description="Check CUDA availability in the reported bench python environment.")
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report, report_err = load_report(report_path)

    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "cuda",
        "task": "check",
        "command": f"{sys.executable} {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "timestamp_utc": utc_timestamp(),
            "python": sys.executable,
            "git_commit": git_commit(root),
            "env_vars": {k: v for k, v in os.environ.items() if k.startswith("SCIMLOPSBENCH_")},
            "decision_reason": "Use python_path from report.json and probe torch/tensorflow/jax for CUDA availability.",
        },
        "observed": {},
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    if report_err:
        msg = f"Report load failed: {report_err} ({report_path})"
        log_path.write_text(msg + "\n", encoding="utf-8")
        results["failure_category"] = "missing_report" if report_err == "missing_report" else "invalid_json"
        results["error_excerpt"] = msg
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path:
        msg = "report.json missing python_path"
        log_path.write_text(msg + "\n", encoding="utf-8")
        results["failure_category"] = "path_hallucination"
        results["error_excerpt"] = msg
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    results["meta"]["reported_python_path"] = python_path
    python_exe = Path(python_path)
    python_ok = python_exe.exists() and os.access(str(python_exe), os.X_OK)
    results["observed"]["python_path_ok"] = bool(python_ok)
    results["observed"]["python_executable"] = python_path

    if not python_ok:
        msg = f"python_path not executable: {python_path}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        results["failure_category"] = "path_hallucination"
        results["error_excerpt"] = msg
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    rc, stdout, stderr = run_python_check(python_path)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[cuda] python_path={python_path}\n")
        if stdout:
            log.write("[cuda] stdout:\n" + stdout + "\n")
        if stderr:
            log.write("[cuda] stderr:\n" + stderr + "\n")

    # Try parse the JSON (stdout last line).
    observed: Dict[str, Any] = {}
    try:
        observed = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except Exception:
        observed = {}

    if isinstance(observed, dict):
        results["framework"] = str(observed.get("framework", "unknown"))
        results["observed"].update(
            {
                "python_version": observed.get("python_version", ""),
                "torch_import_ok": bool(observed.get("torch_import_ok", False)),
                "torch_version": str(observed.get("torch_version", "")),
                "cuda_available": bool(observed.get("cuda_available", False)),
                "gpu_count": int(observed.get("gpu_count", 0)) if str(observed.get("gpu_count", "")).isdigit() else observed.get("gpu_count", 0),
            }
        )

    if rc == 0:
        results["status"] = "success"
        results["exit_code"] = 0
        results["skip_reason"] = "not_applicable"
        results["failure_category"] = "unknown"
        results["error_excerpt"] = ""
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    # CUDA unavailable or probe failed; per spec, exit 1 when CUDA unavailable.
    results["status"] = "failure"
    results["exit_code"] = 1
    results["failure_category"] = "runtime" if rc in (1,) else "unknown"
    results["error_excerpt"] = tail_text(log_path)
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

