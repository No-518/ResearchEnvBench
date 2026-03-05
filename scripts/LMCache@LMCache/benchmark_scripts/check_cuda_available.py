#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:  # noqa: BLE001
        return None, f"invalid_json: {e}"


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def resolve_python(cli_python: Optional[str], report_path: Path) -> Tuple[Optional[str], str, Optional[str]]:
    if cli_python:
        return cli_python, "cli", None
    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return env_python, "env:SCIMLOPSBENCH_PYTHON", None
    report, err = read_json(report_path)
    if report is None:
        return None, "report", f"missing_report:{err}"
    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path:
        return None, "report", "missing_report:python_path_missing"
    return python_path, "report:python_path", None


def tail(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def load_assets_manifest(root: Path) -> Dict[str, Any]:
    manifest_path = root / "benchmark_assets" / "manifest.json"
    data, _err = read_json(manifest_path)
    if not isinstance(data, dict):
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    dataset = data.get("dataset") if isinstance(data.get("dataset"), dict) else {}
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    return {
        "dataset": {
            "path": str(dataset.get("path", "")),
            "source": str(dataset.get("source", "")),
            "version": str(dataset.get("version", "")),
            "sha256": str(dataset.get("sha256", "")),
        },
        "model": {
            "path": str(model.get("path", "")),
            "source": str(model.get("source", "")),
            "version": str(model.get("version", "")),
            "sha256": str(model.get("sha256", "")),
        },
    }


CHECK_SNIPPET = r"""
import json
import sys

result = {
  "framework": "unknown",
  "cuda_available": False,
  "gpu_count": 0,
  "errors": {},
}

def done(exit_code: int):
  sys.stdout.write(json.dumps(result, ensure_ascii=False))
  sys.exit(exit_code)

try:
  import torch
  result["framework"] = "pytorch"
  result["cuda_available"] = bool(torch.cuda.is_available())
  result["gpu_count"] = int(torch.cuda.device_count()) if result["cuda_available"] else 0
  done(0 if result["cuda_available"] else 1)
except Exception as e:
  result["errors"]["pytorch"] = str(e)

try:
  import tensorflow as tf
  result["framework"] = "tensorflow"
  gpus = tf.config.list_physical_devices("GPU")
  result["cuda_available"] = bool(gpus)
  result["gpu_count"] = len(gpus)
  done(0 if result["cuda_available"] else 1)
except Exception as e:
  result["errors"]["tensorflow"] = str(e)

try:
  import jax
  result["framework"] = "jax"
  devs = list(jax.devices())
  gpus = [d for d in devs if getattr(d, "platform", None) == "gpu"]
  result["cuda_available"] = bool(gpus)
  result["gpu_count"] = len(gpus)
  done(0 if result["cuda_available"] else 1)
except Exception as e:
  result["errors"]["jax"] = str(e)

done(1)
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Check CUDA availability using the agent-reported Python environment")
    ap.add_argument("--report-path", default=None)
    ap.add_argument("--python", default=None)
    args = ap.parse_args(argv)

    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    python_path, python_source, python_err = resolve_python(args.python, report_path)

    assets = load_assets_manifest(root)

    status = "failure"
    skip_reason = "unknown"
    exit_code = 1
    framework = "unknown"
    failure_category = "unknown"
    command_str = ""
    observed: Dict[str, Any] = {}

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[cuda] start_utc={utc_timestamp()}\n")
        log_f.write(f"[cuda] report_path={report_path}\n")
        log_f.write(f"[cuda] python_source={python_source}\n")

        if python_err is not None or not python_path:
            log_f.write(f"[cuda] ERROR: failed to resolve python: {python_err}\n")
            failure_category = "missing_report"
        else:
            command = [python_path, "-c", CHECK_SNIPPET]
            command_str = " ".join(shlex.quote(x) for x in command)
            log_f.write(f"[cuda] command={command_str}\n")
            try:
                proc = subprocess.run(
                    command,
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except subprocess.TimeoutExpired as e:
                log_f.write(f"[cuda] TIMEOUT: {e}\n")
                failure_category = "timeout"
            except FileNotFoundError as e:
                log_f.write(f"[cuda] ERROR: python not found/executable: {e}\n")
                failure_category = "path_hallucination"
            except Exception as e:  # noqa: BLE001
                log_f.write(f"[cuda] ERROR: unexpected failure: {e}\n")
                failure_category = "runtime"
            else:
                log_f.write("[cuda] --- subprocess stdout ---\n")
                log_f.write(proc.stdout + "\n")
                log_f.write("[cuda] --- subprocess stderr ---\n")
                log_f.write(proc.stderr + "\n")

                try:
                    observed = json.loads(proc.stdout) if proc.stdout.strip() else {}
                except Exception as e:  # noqa: BLE001
                    observed = {}
                    log_f.write(f"[cuda] ERROR: invalid JSON from probe: {e}\n")
                    failure_category = "invalid_json"
                else:
                    framework = str(observed.get("framework", "unknown"))
                    cuda_available = bool(observed.get("cuda_available", False))
                    status = "success" if cuda_available else "failure"
                    exit_code = 0 if cuda_available else 1
                    failure_category = "unknown" if cuda_available else "runtime"
                    skip_reason = "not_applicable" if cuda_available else "unknown"

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": command_str,
        "timeout_sec": 120,
        "framework": framework,
        "assets": {"dataset": assets["dataset"], "model": assets["model"]},
        "meta": {
            "python": python_path or "",
            "git_commit": "",
            "env_vars": {
                "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
                "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            },
            "decision_reason": "Probes CUDA availability via imports in the agent-reported environment; prefers torch, then tensorflow, then jax.",
            "python_source": python_source,
            "report_path": str(report_path),
        },
        "observed": observed,
        "failure_category": failure_category,
        "error_excerpt": tail(log_path),
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

