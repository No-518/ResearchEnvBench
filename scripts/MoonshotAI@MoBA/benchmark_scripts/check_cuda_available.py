#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).strip()
        return out
    except Exception:
        return ""


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tail(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-max_lines:])
    except Exception:
        return ""


def _load_report(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not report_path.exists():
        return None, f"report not found at {report_path}"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "report JSON is not an object"
        return data, None
    except Exception as e:
        return None, f"failed to read/parse report: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check CUDA availability in the benchmark environment.")
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "cuda"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    report_path = Path(
        args.report_path
        or os.environ.get("SCIMLOPSBENCH_REPORT")
        or "/opt/scimlopsbench/report.json"
    )
    report, report_err = _load_report(report_path)

    base: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "cuda",
        "task": "check",
        "command": "",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "observed": {
            "python_executable": "",
            "framework": "unknown",
            "cuda_available": False,
            "gpu_count": 0,
        },
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_now_iso(),
            "env_vars": {"report_path": str(report_path)},
            "decision_reason": "",
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    if report is None:
        msg = f"Missing/invalid report: {report_err}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        base["meta"]["decision_reason"] = msg
        base["failure_category"] = "missing_report"
        base["error_excerpt"] = msg
        _write_json(results_path, base)
        return 1

    python_path = report.get("python_path")
    if not python_path or not isinstance(python_path, str):
        msg = "report.python_path missing/invalid"
        log_path.write_text(msg + "\n", encoding="utf-8")
        base["meta"]["decision_reason"] = msg
        base["failure_category"] = "missing_report"
        base["error_excerpt"] = msg
        _write_json(results_path, base)
        return 1

    base["observed"]["python_executable"] = python_path
    cmd = [
        python_path,
        "-c",
        r"""
import json

def torch_check():
  try:
    import torch
    return {
      "framework": "pytorch",
      "torch_version": getattr(torch, "__version__", ""),
      "cuda_available": bool(torch.cuda.is_available()),
      "gpu_count": int(torch.cuda.device_count()) if hasattr(torch, "cuda") else 0,
    }
  except Exception as e:
    return {"framework": "pytorch", "error": str(e)}

def tf_check():
  try:
    import tensorflow as tf
    gpus = tf.config.list_physical_devices("GPU")
    return {
      "framework": "tensorflow",
      "tf_version": getattr(tf, "__version__", ""),
      "cuda_available": bool(gpus),
      "gpu_count": int(len(gpus)),
    }
  except Exception as e:
    return {"framework": "tensorflow", "error": str(e)}

def jax_check():
  try:
    import jax
    devices = jax.devices()
    gpu = [d for d in devices if d.platform == "gpu"]
    return {
      "framework": "jax",
      "jax_version": getattr(jax, "__version__", ""),
      "cuda_available": bool(gpu),
      "gpu_count": int(len(gpu)),
    }
  except Exception as e:
    return {"framework": "jax", "error": str(e)}

checks = [torch_check(), tf_check(), jax_check()]

# Prefer torch if import succeeded (even if cuda unavailable).
preferred = None
for c in checks:
  if c.get("framework") == "pytorch" and "error" not in c:
    preferred = c
    break
if preferred is None:
  for c in checks:
    if "error" not in c:
      preferred = c
      break
if preferred is None:
  preferred = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "errors": checks}

print(json.dumps({"preferred": preferred, "all": checks}))
""",
    ]
    base["command"] = " ".join(shlex.quote(x) for x in cmd)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        msg = "CUDA check timed out"
        log_path.write_text(msg + "\n", encoding="utf-8")
        base["failure_category"] = "timeout"
        base["error_excerpt"] = msg
        _write_json(results_path, base)
        return 1
    except FileNotFoundError:
        msg = f"python_path not found/executable: {python_path}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        base["failure_category"] = "path_hallucination"
        base["error_excerpt"] = msg
        _write_json(results_path, base)
        return 1

    log_path.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8", errors="replace")

    if proc.returncode != 0:
        base["failure_category"] = "runtime"
        base["error_excerpt"] = _tail(log_path)
        _write_json(results_path, base)
        return 1

    try:
        payload = json.loads(proc.stdout.strip() or "{}")
    except Exception as e:
        base["failure_category"] = "invalid_json"
        base["error_excerpt"] = f"failed to parse CUDA probe JSON: {e}\n{_tail(log_path)}"
        _write_json(results_path, base)
        return 1

    preferred = payload.get("preferred", {}) if isinstance(payload, dict) else {}
    framework = str(preferred.get("framework") or "unknown")
    cuda_available = bool(preferred.get("cuda_available"))
    gpu_count = int(preferred.get("gpu_count") or 0)

    base["observed"].update(
        {
            "framework": framework,
            "cuda_available": cuda_available,
            "gpu_count": gpu_count,
            "details": payload,
        }
    )
    base["framework"] = framework if framework in {"pytorch", "tensorflow", "jax"} else "unknown"

    if cuda_available:
        base["status"] = "success"
        base["exit_code"] = 0
        base["failure_category"] = "unknown"
        base["error_excerpt"] = ""
        _write_json(results_path, base)
        return 0

    base["status"] = "failure"
    base["exit_code"] = 1
    base["failure_category"] = "insufficient_hardware"
    base["error_excerpt"] = "CUDA unavailable"
    _write_json(results_path, base)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

