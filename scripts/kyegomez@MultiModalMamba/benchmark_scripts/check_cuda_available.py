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


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(txt.splitlines()[-max_lines:])
    except Exception:
        return ""


def empty_assets() -> Dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def load_assets(repo: Path) -> Dict[str, Any]:
    p = repo / "build_output" / "prepare" / "results.json"
    if not p.exists():
        return empty_assets()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        assets = d.get("assets")
        return assets if isinstance(assets, dict) else empty_assets()
    except Exception:
        return empty_assets()


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except Exception as e:
        return None, f"invalid_json: {e}"


def resolve_python(report_path: Path, cli_python: str) -> Tuple[Optional[str], str]:
    if cli_python:
        return cli_python, "cli"
    env_py = os.environ.get("SCIMLOPSBENCH_PYTHON", "").strip()
    if env_py:
        return env_py, "env"
    report, err = load_json(report_path)
    if report is None:
        return None, f"missing_report: {err or 'missing'}"
    py = report.get("python_path")
    if isinstance(py, str) and py.strip():
        return py.strip(), "report"
    return None, "missing_report: report.json missing python_path"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH))
    ap.add_argument("--python", default="", help="Explicit python executable (highest priority)")
    ap.add_argument("--timeout-sec", type=int, default=60)
    args = ap.parse_args()

    repo = repo_root()
    out_dir = repo / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    assets = load_assets(repo)

    def write_results(payload: Dict[str, Any]) -> None:
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with log_path.open("w", encoding="utf-8") as log:
        report_path = Path(args.report_path)
        py, py_source = resolve_python(report_path, args.python)

        if not py:
            log.write(f"[cuda] python resolution failed: {py_source}\n")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "cuda",
                "task": "check",
                "command": "python -c <cuda_check> (not run)",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": assets,
                "meta": {
                    "python": "",
                    "git_commit": "",
                    "env_vars": {
                        "SCIMLOPSBENCH_REPORT": str(report_path),
                        "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
                    },
                    "decision_reason": "CUDA check requires python_path from the agent report.",
                    "timestamp_utc": utcnow(),
                },
                "observed": {"cuda_available": False, "gpu_count": 0},
                "failure_category": "missing_report",
                "error_excerpt": py_source,
            }
            write_results(payload)
            return 1

        check_code = r"""
import json, platform, sys
out = {
  "framework": "unknown",
  "python_executable": sys.executable,
  "python_version": platform.python_version(),
  "cuda_available": False,
  "gpu_count": 0,
  "details": {},
}
errors = {}
try:
  import torch
  out["framework"] = "pytorch"
  out["details"]["torch_version"] = getattr(torch, "__version__", "")
  out["cuda_available"] = bool(torch.cuda.is_available())
  out["gpu_count"] = int(torch.cuda.device_count() if torch.cuda.is_available() else 0)
except Exception as e:
  errors["torch"] = repr(e)

if out["framework"] == "unknown":
  try:
    import tensorflow as tf
    gpus = tf.config.list_physical_devices("GPU")
    out["framework"] = "tensorflow"
    out["details"]["tf_version"] = getattr(tf, "__version__", "")
    out["cuda_available"] = bool(gpus)
    out["gpu_count"] = int(len(gpus))
  except Exception as e:
    errors["tensorflow"] = repr(e)

if out["framework"] == "unknown":
  try:
    import jax
    devs = jax.devices()
    out["framework"] = "jax"
    out["details"]["jax_version"] = getattr(jax, "__version__", "")
    out["details"]["jax_platforms"] = [getattr(d, "platform", "") for d in devs]
    out["cuda_available"] = any(getattr(d, "platform", "") == "gpu" for d in devs)
    out["gpu_count"] = int(sum(1 for d in devs if getattr(d, "platform", "") == "gpu"))
  except Exception as e:
    errors["jax"] = repr(e)

out["details"]["import_errors"] = errors
print(json.dumps(out))
""".strip()

        cmd = [py, "-c", check_code]
        log.write(f"[cuda] python_source={py_source}\n")
        log.write(f"[cuda] cmd={' '.join(cmd[:2])} -c <code>\n")
        log.flush()

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=args.timeout_sec,
            )
        except subprocess.TimeoutExpired:
            log.write("[cuda] timeout\n")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "cuda",
                "task": "check",
                "command": f"{py} -c <cuda_check>",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": assets,
                "meta": {
                    "python": py,
                    "git_commit": "",
                    "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "")},
                    "decision_reason": "CUDA availability check timed out.",
                    "timestamp_utc": utcnow(),
                },
                "observed": {"cuda_available": False, "gpu_count": 0},
                "failure_category": "timeout",
                "error_excerpt": "timeout",
            }
            write_results(payload)
            return 1

        log.write(proc.stdout or "")
        log.flush()

        try:
            observed = json.loads((proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            observed = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "details": {}}

        cuda_ok = bool(observed.get("cuda_available"))
        framework = str(observed.get("framework") or "unknown")

        payload = {
            "status": "success" if cuda_ok else "failure",
            "skip_reason": "unknown",
            "exit_code": 0 if cuda_ok else 1,
            "stage": "cuda",
            "task": "check",
            "command": f"{py} -c <cuda_check>",
            "timeout_sec": 120,
            "framework": framework if framework in {"pytorch", "tensorflow", "jax"} else "unknown",
            "assets": assets,
            "meta": {
                "python": py,
                "git_commit": "",
                "env_vars": {
                    "SCIMLOPSBENCH_REPORT": str(report_path),
                    "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
                    "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
                },
                "decision_reason": "Detect CUDA via torch/tensorflow/jax in the report python environment.",
                "timestamp_utc": utcnow(),
                "python_resolution_source": py_source,
            },
            "observed": {
                "python_executable": observed.get("python_executable", ""),
                "python_version": observed.get("python_version", ""),
                "cuda_available": cuda_ok,
                "gpu_count": int(observed.get("gpu_count") or 0),
                "details": observed.get("details", {}),
            },
            "failure_category": "runtime" if not cuda_ok else "unknown",
            "error_excerpt": tail_lines(log_path, max_lines=220) if not cuda_ok else "",
        }
        write_results(payload)
        return 0 if cuda_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

