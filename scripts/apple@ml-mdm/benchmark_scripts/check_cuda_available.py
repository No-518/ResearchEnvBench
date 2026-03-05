#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, ""
    except FileNotFoundError:
        return None, "missing_report"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "unknown"


def report_path_from_args(cli_report_path: str) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(DEFAULT_REPORT_PATH)


def read_git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).strip()
        return out
    except Exception:
        return ""


def empty_assets() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


CUDA_PROBE_CODE = r"""
import json, sys

out = {
  "framework": "unknown",
  "cuda_available": False,
  "gpu_count": 0,
  "details": {},
}

def finish(code: int):
  print(json.dumps(out))
  raise SystemExit(code)

try:
  import torch
  out["framework"] = "pytorch"
  out["details"]["torch_version"] = getattr(torch, "__version__", "")
  out["cuda_available"] = bool(torch.cuda.is_available())
  out["gpu_count"] = int(torch.cuda.device_count()) if out["cuda_available"] else 0
  finish(0 if out["cuda_available"] else 1)
except Exception as e:
  out["details"]["torch_error"] = str(e)

try:
  import tensorflow as tf
  out["framework"] = "tensorflow"
  gpus = tf.config.list_physical_devices("GPU")
  out["cuda_available"] = bool(gpus)
  out["gpu_count"] = len(gpus)
  out["details"]["tf_version"] = getattr(tf, "__version__", "")
  finish(0 if out["cuda_available"] else 1)
except Exception as e:
  out["details"]["tf_error"] = str(e)

try:
  import jax
  out["framework"] = "jax"
  devices = jax.devices()
  gpus = [d for d in devices if getattr(d, "platform", "") == "gpu"]
  out["cuda_available"] = bool(gpus)
  out["gpu_count"] = len(gpus)
  out["details"]["jax_version"] = getattr(jax, "__version__", "")
  finish(0 if out["cuda_available"] else 1)
except Exception as e:
  out["details"]["jax_error"] = str(e)

finish(1)
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Check CUDA availability using report.json python_path.")
    ap.add_argument("--report-path", default="", help="Override /opt/scimlopsbench/report.json")
    ap.add_argument("--timeout-sec", type=int, default=120)
    args = ap.parse_args(argv)

    out_dir = REPO_ROOT / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    report_path = report_path_from_args(args.report_path)
    report, report_err = load_json(report_path)

    base: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "cuda",
        "task": "check",
        "command": "",
        "timeout_sec": int(args.timeout_sec),
        "framework": "unknown",
        "assets": empty_assets(),
        "meta": {
            "python": f"{sys.executable} ({platform.python_version()})",
            "git_commit": read_git_commit(),
            "env_vars": {k: os.environ.get(k, "") for k in ["SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON"] if os.environ.get(k)},
            "decision_reason": "CUDA availability is checked via the python_path recorded in the agent report.json to match the benchmark environment.",
            "timestamp_utc": now_utc_iso(),
            "report_path": str(report_path),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
        "observed": {},
    }

    if report is None:
        msg = f"failed_to_load_report: {report_err} ({report_path})\n"
        log_path.write_text(msg, encoding="utf-8")
        base["failure_category"] = "missing_report" if report_err in ("missing_report", "invalid_json") else "unknown"
        base["command"] = ""
        base["error_excerpt"] = tail_lines(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    python_path = str(report.get("python_path") or "").strip()
    if not python_path:
        log_path.write_text("report.json missing python_path\n", encoding="utf-8")
        base["failure_category"] = "missing_report"
        base["command"] = ""
        base["error_excerpt"] = tail_lines(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    cmd = [python_path, "-c", CUDA_PROBE_CODE]
    base["command"] = " ".join([python_path, "-c", "<cuda_probe_code>"])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=int(args.timeout_sec),
        )
        log_path.write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        log_path.write_text(f"timeout_after_{args.timeout_sec}_sec\n", encoding="utf-8")
        base["failure_category"] = "timeout"
        base["error_excerpt"] = tail_lines(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1
    except FileNotFoundError as e:
        log_path.write_text(f"python_path_not_found: {e}\n", encoding="utf-8")
        base["failure_category"] = "path_hallucination"
        base["error_excerpt"] = tail_lines(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1
    except Exception as e:
        log_path.write_text(f"unexpected_error: {e}\n", encoding="utf-8")
        base["failure_category"] = "unknown"
        base["error_excerpt"] = tail_lines(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    observed: Dict[str, Any] = {}
    try:
        observed = json.loads(proc.stdout.strip() or "{}")
        if not isinstance(observed, dict):
            observed = {}
    except Exception:
        observed = {}

    base["observed"] = observed
    base["framework"] = str(observed.get("framework") or "unknown")

    cuda_available = bool(observed.get("cuda_available"))
    gpu_count = int(observed.get("gpu_count") or 0)
    base["meta"]["python_path"] = python_path
    base["meta"]["reported"] = {
        "cuda_available": report.get("cuda_available", None),
        "gpu_count": report.get("gpu_count", None),
    }

    if cuda_available:
        base["status"] = "success"
        base["exit_code"] = 0
        base["failure_category"] = "unknown"
        base["skip_reason"] = "not_applicable"
    else:
        base["status"] = "failure"
        base["exit_code"] = 1
        base["failure_category"] = "runtime"
        base["skip_reason"] = "insufficient_hardware"
        base["meta"]["cuda_details"] = {"gpu_count": gpu_count}

    base["error_excerpt"] = tail_lines(log_path)
    results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if cuda_available else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

