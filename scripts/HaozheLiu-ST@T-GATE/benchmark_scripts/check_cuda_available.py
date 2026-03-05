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


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_report_path(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("json_not_object")
    return data


def tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    result: dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "cuda",
        "task": "check",
        "command": "benchmark_scripts/check_cuda_available.py",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": sys.executable,
            "git_commit": "",
            "env_vars": {},
            "decision_reason": "Check CUDA availability using the agent-reported python environment.",
            "timestamp_utc": utc_now_iso(),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    try:
        report_path = resolve_report_path(args.report_path)
        result["meta"]["report_path"] = str(report_path)
        report = read_json(report_path)
        python_path = report.get("python_path")
        if not isinstance(python_path, str) or not python_path:
            raise RuntimeError("report missing python_path")
        if not (os.path.isfile(python_path) and os.access(python_path, os.X_OK)):
            raise RuntimeError(f"python_path not executable: {python_path}")

        probe = r"""
import json
out = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "errors": {}}
try:
    import torch
    out["framework"] = "pytorch"
    out["torch_version"] = getattr(torch, "__version__", "")
    out["cuda_available"] = bool(torch.cuda.is_available())
    out["gpu_count"] = int(torch.cuda.device_count() if torch.cuda.is_available() else 0)
except Exception as e:
    out["errors"]["torch"] = repr(e)

if out["framework"] == "unknown":
    try:
        import tensorflow as tf
        gpus = tf.config.list_physical_devices("GPU")
        out["framework"] = "tensorflow"
        out["cuda_available"] = bool(gpus)
        out["gpu_count"] = int(len(gpus))
        out["tf_version"] = getattr(tf, "__version__", "")
    except Exception as e:
        out["errors"]["tensorflow"] = repr(e)

if out["framework"] == "unknown":
    try:
        import jax
        devs = jax.devices()
        out["framework"] = "jax"
        out["cuda_available"] = any(getattr(d, "platform", "") == "gpu" for d in devs)
        out["gpu_count"] = int(sum(1 for d in devs if getattr(d, "platform", "") == "gpu"))
        out["jax_version"] = getattr(jax, "__version__", "")
    except Exception as e:
        out["errors"]["jax"] = repr(e)

print(json.dumps(out))
"""
        with log_path.open("w", encoding="utf-8", errors="replace") as lf:
            lf.write(f"[cuda] report_path={report_path}\n")
            lf.write(f"[cuda] python_path={python_path}\n")
            lf.write(f"[cuda] timestamp_utc={utc_now_iso()}\n")
            lf.flush()
            p = subprocess.run(
                [python_path, "-c", probe],
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=120,
            )
            lf.write(p.stdout)
            lf.write(f"\n[cuda] probe_returncode={p.returncode}\n")

        observed: dict[str, Any] = {}
        try:
            observed = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            observed = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "errors": {"parse": "failed"}}

        result["framework"] = observed.get("framework", "unknown")
        result["observed"] = {
            "cuda_available": bool(observed.get("cuda_available", False)),
            "gpu_count": int(observed.get("gpu_count", 0) or 0),
        }

        if result["observed"]["cuda_available"]:
            result["status"] = "success"
            result["exit_code"] = 0
            result["failure_category"] = ""
        else:
            result["status"] = "failure"
            result["exit_code"] = 1
            result["failure_category"] = "insufficient_hardware"

        result["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if result["exit_code"] == 0 else 1
    except Exception as e:
        with log_path.open("a", encoding="utf-8", errors="replace") as lf:
            lf.write("\n[cuda] exception:\n")
            lf.write(str(e) + "\n")
            lf.write(traceback.format_exc())
        result["status"] = "failure"
        result["exit_code"] = 1
        msg = str(e)
        if "missing python_path" in msg or "report" in msg:
            result["failure_category"] = "missing_report"
        else:
            result["failure_category"] = "unknown"
        result["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

