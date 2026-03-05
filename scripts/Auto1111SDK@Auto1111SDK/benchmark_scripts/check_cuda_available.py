#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

sys.dont_write_bytecode = True


REPO_ROOT = Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        sha = (cp.stdout or "").strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_report = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_report:
        return Path(env_report)
    return Path("/opt/scimlopsbench/report.json")


def _read_report(report_path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = report_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception as e:
        return None, f"missing_report: {type(e).__name__}: {e}"
    try:
        obj = json.loads(raw)
    except Exception as e:
        return None, f"invalid_json: {type(e).__name__}: {e}"
    if not isinstance(obj, dict):
        return None, "invalid_json: top-level is not an object"
    return obj, None


def _resolve_python(cli_python: Optional[str], report_path: Path) -> Tuple[Optional[str], str, list[str], Optional[dict]]:
    warnings: list[str] = []
    if cli_python:
        return cli_python, "cli", warnings, None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return env_python, "env", warnings, None

    report, err = _read_report(report_path)
    if err:
        return None, "report", warnings, None

    python_path = report.get("python_path")
    if isinstance(python_path, str) and python_path.strip():
        p = python_path.strip()
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p, "report", warnings, report
        warnings.append(f"reported python_path is not executable; falling back to PATH python: {p}")

    fallback = shutil.which("python") or shutil.which("python3")
    if fallback:
        warnings.append("using PATH python fallback")
        return fallback, "path", warnings, report

    return None, "path", warnings, report


def _env_snapshot() -> Dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_PYTHON",
        "SCIMLOPSBENCH_REPORT",
        "HF_HOME",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "WANDB_MODE",
    ]
    snap: Dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            snap[k] = v
    return snap


def _probe_cuda_via_python(python_exe: str, timeout_sec: int = 60) -> Tuple[Optional[dict], Optional[str]]:
    probe = r"""
import json
out = {
  "framework": "unknown",
  "torch": {"import_ok": False, "cuda_available": None, "gpu_count": None, "version": None},
  "tensorflow": {"import_ok": False, "cuda_available": None, "gpu_count": None, "version": None},
  "jax": {"import_ok": False, "cuda_available": None, "gpu_count": None, "version": None},
}

def done(framework: str, cuda_available, gpu_count):
  out["framework"] = framework
  out["cuda_available"] = bool(cuda_available)
  out["gpu_count"] = int(gpu_count) if gpu_count is not None else 0
  print(json.dumps(out))

try:
  import torch
  out["torch"]["import_ok"] = True
  out["torch"]["version"] = getattr(torch, "__version__", None)
  ca = bool(torch.cuda.is_available())
  gc = int(torch.cuda.device_count()) if ca else int(torch.cuda.device_count())
  out["torch"]["cuda_available"] = ca
  out["torch"]["gpu_count"] = gc
  done("pytorch", ca, gc)
except Exception as e:
  out["torch"]["error"] = f"{type(e).__name__}: {e}"

try:
  import tensorflow as tf
  out["tensorflow"]["import_ok"] = True
  out["tensorflow"]["version"] = getattr(tf, "__version__", None)
  gpus = list(getattr(tf.config, "list_physical_devices")("GPU"))
  gc = len(gpus)
  out["tensorflow"]["gpu_count"] = gc
  out["tensorflow"]["cuda_available"] = gc > 0
  if out.get("framework") == "unknown":
    done("tensorflow", gc > 0, gc)
except Exception as e:
  out["tensorflow"]["error"] = f"{type(e).__name__}: {e}"

try:
  import jax
  out["jax"]["import_ok"] = True
  out["jax"]["version"] = getattr(jax, "__version__", None)
  devs = list(jax.devices())
  gpu_devs = [d for d in devs if getattr(d, "platform", "") == "gpu"]
  gc = len(gpu_devs)
  out["jax"]["gpu_count"] = gc
  out["jax"]["cuda_available"] = gc > 0
  if out.get("framework") == "unknown":
    done("jax", gc > 0, gc)
except Exception as e:
  out["jax"]["error"] = f"{type(e).__name__}: {e}"

if out.get("framework") == "unknown":
  out["cuda_available"] = False
  out["gpu_count"] = 0
  print(json.dumps(out))
"""
    try:
        cp = subprocess.run(
            [python_exe, "-c", probe],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if cp.returncode != 0:
        return None, (cp.stderr or cp.stdout or "").strip()[-4000:]
    try:
        obj = json.loads((cp.stdout or "").strip() or "{}")
    except Exception as e:
        return None, f"invalid_probe_json: {type(e).__name__}: {e}"
    if not isinstance(obj, dict):
        return None, "invalid_probe_json: top-level is not an object"
    return obj, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=None, help="Override python executable for probing")
    ap.add_argument("--report-path", default=None, help="Override report.json path")
    args = ap.parse_args()

    out_dir = REPO_ROOT / "build_output" / "cuda"
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(line: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")

    report_path = _resolve_report_path(args.report_path)
    log(f"[cuda] start_utc={_utc_now_iso()}")
    log(f"[cuda] report_path={report_path}")

    python_exe, python_source, python_warnings, report = _resolve_python(args.python, report_path)
    if not python_exe:
        payload = {
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
                "python": "",
                "git_commit": _git_commit(REPO_ROOT),
                "env_vars": _env_snapshot(),
                "decision_reason": "Failed to resolve python for CUDA probe (missing/invalid report.json and no --python override).",
                "report_path": str(report_path),
                "python_resolution_warnings": python_warnings,
            },
            "observed": {"cuda_available": False, "gpu_count": 0},
            "failure_category": "missing_report",
            "error_excerpt": f"missing/invalid report.json: {report_path}",
        }
        _write_json(results_path, payload)
        return 1

    log(f"[cuda] python_exe={python_exe} (source={python_source})")
    for w in python_warnings:
        log(f"[cuda] warning: {w}")

    probe, probe_err = _probe_cuda_via_python(python_exe)
    if probe_err:
        log(f"[cuda] probe_error={probe_err}")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "cuda",
            "task": "check",
            "command": f"{python_exe} -c <cuda_probe>",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "meta": {
                "python": python_exe,
                "git_commit": _git_commit(REPO_ROOT),
                "env_vars": _env_snapshot(),
                "decision_reason": "Probe CUDA availability in the benchmark python environment.",
                "report_path": str(report_path),
                "python_resolution_warnings": python_warnings,
            },
            "observed": {"cuda_available": False, "gpu_count": 0},
            "failure_category": "runtime",
            "error_excerpt": probe_err[-4000:],
        }
        _write_json(results_path, payload)
        return 1

    framework = probe.get("framework", "unknown") if isinstance(probe, dict) else "unknown"
    cuda_available = bool(probe.get("cuda_available", False))
    gpu_count = int(probe.get("gpu_count", 0) or 0)

    log(f"[cuda] framework={framework} cuda_available={cuda_available} gpu_count={gpu_count}")
    status = "success" if cuda_available else "failure"
    exit_code = 0 if cuda_available else 1

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": f"{python_exe} -c <cuda_probe>",
        "timeout_sec": 120,
        "framework": framework if framework in ("pytorch", "tensorflow", "jax") else "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": python_exe,
            "git_commit": _git_commit(REPO_ROOT),
            "env_vars": _env_snapshot(),
            "decision_reason": "Probe CUDA availability in the benchmark python environment.",
            "report_path": str(report_path),
            "python_source": python_source,
            "python_resolution_warnings": python_warnings,
        },
        "observed": {
            "cuda_available": cuda_available,
            "gpu_count": gpu_count,
            "probe": probe,
        },
        "failure_category": "unknown" if cuda_available else "insufficient_hardware",
        "error_excerpt": "" if cuda_available else "CUDA is not available (torch/tf/jax probe did not find usable GPUs).",
    }
    _write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
