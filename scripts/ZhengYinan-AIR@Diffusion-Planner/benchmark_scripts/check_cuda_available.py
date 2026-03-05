#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def stage_dir() -> Path:
    return repo_root() / "build_output" / "cuda"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_report_path(cli_report_path: Optional[str]) -> str:
    if cli_report_path:
        return cli_report_path
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return os.environ["SCIMLOPSBENCH_REPORT"]
    return DEFAULT_REPORT_PATH


def resolve_python(cli_python: Optional[str], report_path: str) -> Optional[str]:
    if cli_python:
        return cli_python
    if os.environ.get("SCIMLOPSBENCH_PYTHON"):
        return os.environ["SCIMLOPSBENCH_PYTHON"]
    p = Path(report_path)
    if not p.exists():
        return None
    try:
        data = load_json(p)
    except Exception:
        return None
    py = data.get("python_path")
    if isinstance(py, str) and py.strip():
        return py
    return None


def run_probe(python_exe: str) -> Dict[str, Any]:
    probe_code = r"""
import json
out = {
  "python_executable": None,
  "python_version": None,
  "torch": {"import_ok": False, "version": "", "cuda_available": False, "gpu_count": 0},
  "tensorflow": {"import_ok": False, "version": "", "gpu_count": 0},
  "jax": {"import_ok": False, "version": "", "gpu_count": 0},
}
try:
  import sys, platform
  out["python_executable"] = sys.executable
  out["python_version"] = platform.python_version()
except Exception:
  pass

try:
  import torch
  out["torch"]["import_ok"] = True
  out["torch"]["version"] = getattr(torch, "__version__", "")
  try:
    out["torch"]["cuda_available"] = bool(torch.cuda.is_available())
    out["torch"]["gpu_count"] = int(torch.cuda.device_count() if out["torch"]["cuda_available"] else 0)
  except Exception:
    out["torch"]["cuda_available"] = False
    out["torch"]["gpu_count"] = 0
except Exception:
  pass

try:
  import tensorflow as tf
  out["tensorflow"]["import_ok"] = True
  out["tensorflow"]["version"] = getattr(tf, "__version__", "")
  try:
    gpus = tf.config.list_physical_devices("GPU")
    out["tensorflow"]["gpu_count"] = int(len(gpus))
  except Exception:
    out["tensorflow"]["gpu_count"] = 0
except Exception:
  pass

try:
  import jax
  out["jax"]["import_ok"] = True
  out["jax"]["version"] = getattr(jax, "__version__", "")
  try:
    devs = jax.devices()
    out["jax"]["gpu_count"] = int(sum(1 for d in devs if getattr(d, "platform", "") == "gpu"))
  except Exception:
    out["jax"]["gpu_count"] = 0
except Exception:
  pass

print(json.dumps(out))
"""
    proc = subprocess.run(
        [python_exe, "-c", probe_code],
        cwd=str(repo_root()),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"probe failed rc={proc.returncode}")
    return json.loads(proc.stdout.strip() or "{}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check CUDA availability in the benchmarked Python environment.")
    parser.add_argument("--report-path", default=None, help="Override report path.")
    parser.add_argument("--python", default=None, help="Override python executable for the probe.")
    args = parser.parse_args()

    out_dir = stage_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    python_exe = resolve_python(args.python, report_path)

    log_lines = []
    log_lines.append(f"[cuda] timestamp_utc={utc_now_iso()}")
    log_lines.append(f"[cuda] report_path={report_path}")
    log_lines.append(f"[cuda] python_exe={python_exe or ''}")

    results: Dict[str, Any] = {
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
        "meta": {
            "git_commit": "",
            "timestamp_utc": utc_now_iso(),
            "env_vars": {k: v for k, v in os.environ.items() if k.startswith("SCIMLOPSBENCH_") or k.startswith("CUDA")},
            "decision_reason": "Probe CUDA availability via torch/tensorflow/jax in the report python environment.",
        },
        "observed": {},
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    if not python_exe:
        msg = "Unable to resolve python for CUDA probe (missing report and no override)."
        log_lines.append(f"[cuda] ERROR: {msg}")
        results.update(
            {
                "failure_category": "missing_report",
                "error_excerpt": msg,
                "command": f"<python unresolved> -c <probe>",
            }
        )
        write_text(log_path, "\n".join(log_lines) + "\n")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    results["command"] = f"{python_exe} -c <cuda_probe>"

    try:
        observed = run_probe(python_exe)
        results["observed"] = observed
        log_lines.append("[cuda] probe_ok=true")
        log_lines.append(f"[cuda] observed={json.dumps(observed)}")
    except Exception as e:
        msg = str(e)
        log_lines.append("[cuda] probe_ok=false")
        log_lines.append(f"[cuda] ERROR: {msg}")
        results.update(
            {
                "failure_category": "runtime",
                "error_excerpt": msg,
            }
        )
        write_text(log_path, "\n".join(log_lines) + "\n")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    framework = "unknown"
    cuda_available = False
    gpu_count = 0
    if observed.get("torch", {}).get("import_ok"):
        framework = "pytorch"
        cuda_available = bool(observed["torch"].get("cuda_available", False))
        gpu_count = int(observed["torch"].get("gpu_count", 0) or 0)
    elif observed.get("tensorflow", {}).get("import_ok"):
        framework = "tensorflow"
        gpu_count = int(observed["tensorflow"].get("gpu_count", 0) or 0)
        cuda_available = gpu_count > 0
    elif observed.get("jax", {}).get("import_ok"):
        framework = "jax"
        gpu_count = int(observed["jax"].get("gpu_count", 0) or 0)
        cuda_available = gpu_count > 0

    results["framework"] = framework
    results["observed"]["framework_selected"] = framework
    results["observed"]["cuda_available"] = cuda_available
    results["observed"]["gpu_count"] = gpu_count

    if cuda_available and gpu_count > 0:
        results.update({"status": "success", "exit_code": 0, "failure_category": "unknown", "error_excerpt": ""})
        write_text(log_path, "\n".join(log_lines) + "\n")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0

    msg = f"CUDA not available (framework={framework}, gpu_count={gpu_count})."
    log_lines.append(f"[cuda] {msg}")
    results.update({"status": "failure", "exit_code": 1, "failure_category": "runtime", "error_excerpt": msg})
    write_text(log_path, "\n".join(log_lines) + "\n")
    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

