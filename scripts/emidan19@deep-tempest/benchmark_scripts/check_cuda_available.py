#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
STAGE = "cuda"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_tail(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def git_commit() -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:
        return ""


def resolve_python(cli_python: Optional[str], report_path: Optional[str]) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    sys.path.insert(0, str((REPO_ROOT / "benchmark_scripts").resolve()))
    import runner  # type: ignore

    rp = runner.resolve_report_path(report_path)
    res, err = runner.resolve_python(cli_python=cli_python, report_path=rp)
    meta: Dict[str, Any] = {"report_path": str(rp)}
    if res is None:
        return None, meta, err or "missing_report"
    meta["python_resolution"] = {
        "python": res.python,
        "source": res.source,
        "warnings": res.warnings,
        "report_python_path": res.report_python_path,
    }
    return res.python, meta, None


def run_probe(python_exe: str, timeout_sec: int) -> Tuple[int, str]:
    code = r"""
import json

out = {
  "framework": "unknown",
  "cuda_available": False,
  "gpu_count": 0,
  "torch_version": "",
  "tf_version": "",
  "jax_version": "",
  "error": ""
}

def finish():
  print(json.dumps(out))

try:
  import torch
  out["framework"] = "pytorch"
  out["torch_version"] = getattr(torch, "__version__", "")
  out["cuda_available"] = bool(getattr(torch.cuda, "is_available", lambda: False)())
  out["gpu_count"] = int(getattr(torch.cuda, "device_count", lambda: 0)())
  finish()
  raise SystemExit(0)
except SystemExit:
  raise
except Exception as e:
  out["error"] = f"torch_import_failed:{type(e).__name__}:{e}"

try:
  import tensorflow as tf
  out["framework"] = "tensorflow"
  out["tf_version"] = getattr(tf, "__version__", "")
  gpus = []
  try:
    gpus = tf.config.list_physical_devices("GPU")
  except Exception:
    gpus = []
  out["gpu_count"] = int(len(gpus))
  out["cuda_available"] = out["gpu_count"] > 0
  finish()
  raise SystemExit(0)
except SystemExit:
  raise
except Exception as e:
  out["error"] = (out["error"] + "; " if out["error"] else "") + f"tf_import_failed:{type(e).__name__}:{e}"

try:
  import jax
  out["framework"] = "jax"
  out["jax_version"] = getattr(jax, "__version__", "")
  try:
    devices = jax.devices()
    out["gpu_count"] = int(sum(1 for d in devices if d.platform in ("gpu", "cuda")))
  except Exception:
    out["gpu_count"] = 0
  out["cuda_available"] = out["gpu_count"] > 0
  finish()
  raise SystemExit(0)
except SystemExit:
  raise
except Exception as e:
  out["error"] = (out["error"] + "; " if out["error"] else "") + f"jax_import_failed:{type(e).__name__}:{e}"

finish()
"""
    cp = subprocess.run(
        [python_exe, "-c", code],
        cwd=REPO_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec,
        env=dict(os.environ),
    )
    return int(cp.returncode), cp.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", help="Override python executable (else uses SCIMLOPSBENCH_PYTHON or report.json python_path)")
    ap.add_argument("--report-path", help="Override report.json path (else SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)")
    ap.add_argument("--timeout-sec", type=int, default=int(os.environ.get("SCIMLOPSBENCH_CUDA_TIMEOUT_SEC", "120")))
    args = ap.parse_args()

    out_dir = REPO_ROOT / "build_output" / STAGE
    safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    meta: Dict[str, Any] = {"python": sys.executable, "git_commit": git_commit(), "env_vars": {}, "timestamp_utc": utc_now()}
    assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

    try:
        py, py_meta, err = resolve_python(args.python, args.report_path)
        meta.update(py_meta)
        if py is None:
            log_path.write_text(f"[cuda] failed to resolve python: {err}\n", encoding="utf-8")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": STAGE,
                "task": "check",
                "command": "",
                "timeout_sec": int(args.timeout_sec),
                "framework": "unknown",
                "assets": assets,
                "meta": {**meta, "decision_reason": "CUDA check requires the benchmark python resolved from the agent report.", "error": err},
                "observed": {"cuda_available": False, "gpu_count": 0},
                "failure_category": "missing_report",
                "error_excerpt": read_tail(log_path),
            }
            results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            return 1

        meta["meta_python_for_probe"] = py
        cmd = f"{py} -c <probe>"
        log_lines = [f"[cuda] timestamp_utc={utc_now()}", f"[cuda] probe_python={py}"]
        rc, out = run_probe(py, timeout_sec=int(args.timeout_sec))
        log_lines.append(f"[cuda] probe_rc={rc}")
        log_lines.append(out)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

        observed: Dict[str, Any] = {"cuda_available": False, "gpu_count": 0, "framework": "unknown"}
        failure_category = "unknown"
        status = "failure"
        exit_code = 1
        framework = "unknown"

        try:
            data = json.loads(out.strip().splitlines()[-1])
            if isinstance(data, dict):
                observed.update(
                    {
                        "framework": data.get("framework", "unknown"),
                        "cuda_available": bool(data.get("cuda_available", False)),
                        "gpu_count": int(data.get("gpu_count", 0) or 0),
                        "torch_version": data.get("torch_version", ""),
                        "tf_version": data.get("tf_version", ""),
                        "jax_version": data.get("jax_version", ""),
                        "probe_error": data.get("error", ""),
                    }
                )
        except Exception as e:
            observed["probe_parse_error"] = f"{type(e).__name__}:{e}"

        framework = observed.get("framework", "unknown") or "unknown"

        if framework == "unknown":
            failure_category = "deps"
        else:
            if observed.get("cuda_available"):
                status = "success"
                exit_code = 0
                failure_category = "not_applicable"
            else:
                failure_category = "insufficient_hardware"

        payload = {
            "status": status,
            "skip_reason": "not_applicable",
            "exit_code": exit_code,
            "stage": STAGE,
            "task": "check",
            "command": cmd,
            "timeout_sec": int(args.timeout_sec),
            "framework": framework if framework in ("pytorch", "tensorflow", "jax") else "unknown",
            "assets": assets,
            "meta": {
                **meta,
                "decision_reason": "Probe CUDA availability using the benchmark python resolved from the agent report, trying torch -> tensorflow -> jax.",
            },
            "observed": observed,
            "failure_category": failure_category,
            "error_excerpt": read_tail(log_path),
        }
        results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 0 if exit_code == 0 else 1
    except subprocess.TimeoutExpired:
        log_path.write_text("[cuda] probe timed out\n", encoding="utf-8")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": STAGE,
            "task": "check",
            "command": "",
            "timeout_sec": int(args.timeout_sec),
            "framework": "unknown",
            "assets": assets,
            "meta": {**meta, "decision_reason": "CUDA probe timed out."},
            "observed": {"cuda_available": False, "gpu_count": 0},
            "failure_category": "timeout",
            "error_excerpt": read_tail(log_path),
        }
        results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 1
    except Exception as e:
        log_path.write_text(f"[cuda] unexpected error: {type(e).__name__}: {e}\n", encoding="utf-8")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": STAGE,
            "task": "check",
            "command": "",
            "timeout_sec": int(args.timeout_sec),
            "framework": "unknown",
            "assets": assets,
            "meta": {**meta, "decision_reason": "Unexpected failure in CUDA probe."},
            "observed": {"cuda_available": False, "gpu_count": 0},
            "failure_category": "unknown",
            "error_excerpt": read_tail(log_path),
        }
        results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

