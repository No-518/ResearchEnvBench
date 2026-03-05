#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _tail_lines(text: str, max_lines: int = 220) -> str:
    lines = text.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)


def _safe_json_load(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json in {path}: {e}"
    except Exception as e:
        return None, f"failed to read {path}: {e}"


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def _resolve_python(cli_python: Optional[str], report_path: Path) -> Tuple[Optional[str], str, Optional[str]]:
    if cli_python:
        return cli_python, "cli", None

    env_py = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_py:
        return env_py, "env", None

    report, err = _safe_json_load(report_path)
    if report is None:
        return None, "missing_report", err

    py = report.get("python_path")
    if isinstance(py, str) and py.strip():
        return py, "report", None

    fallback = "python"
    return fallback, "fallback", 'report.json missing "python_path"; using fallback python from PATH'


def _quote_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


PROBE_CODE = r"""
import json

out = {
  "framework": "unknown",
  "torch": {"import_ok": False, "cuda_available": False, "gpu_count": 0, "error": None},
  "tensorflow": {"import_ok": False, "cuda_available": False, "gpu_count": 0, "error": None},
  "jax": {"import_ok": False, "cuda_available": False, "gpu_count": 0, "error": None},
}

try:
    import torch
    out["torch"]["import_ok"] = True
    out["torch"]["cuda_available"] = bool(torch.cuda.is_available())
    out["torch"]["gpu_count"] = int(torch.cuda.device_count()) if out["torch"]["cuda_available"] else int(torch.cuda.device_count())
except Exception as e:
    out["torch"]["error"] = str(e)

try:
    import tensorflow as tf
    out["tensorflow"]["import_ok"] = True
    gpus = tf.config.list_physical_devices("GPU")
    out["tensorflow"]["gpu_count"] = len(gpus)
    out["tensorflow"]["cuda_available"] = bool(gpus)
except Exception as e:
    out["tensorflow"]["error"] = str(e)

try:
    import jax
    out["jax"]["import_ok"] = True
    devs = getattr(jax, "devices", lambda: [])()
    gpus = [d for d in devs if getattr(d, "platform", "") == "gpu"]
    out["jax"]["gpu_count"] = len(gpus)
    out["jax"]["cuda_available"] = bool(gpus)
except Exception as e:
    out["jax"]["error"] = str(e)

# prefer torch, then tensorflow, then jax
if out["torch"]["import_ok"]:
    out["framework"] = "pytorch"
elif out["tensorflow"]["import_ok"]:
    out["framework"] = "tensorflow"
elif out["jax"]["import_ok"]:
    out["framework"] = "jax"

print(json.dumps(out))
""".strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None)
    ap.add_argument("--python", default=None)
    ap.add_argument("--timeout-sec", type=int, default=120)
    args = ap.parse_args()

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "cuda"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"

    report_path = _resolve_report_path(args.report_path)
    py, py_source, py_warn = _resolve_python(args.python, report_path)

    log_lines: list[str] = []
    log_lines.append(f"[cuda] start_utc={_utc_now_iso()}")
    log_lines.append(f"[cuda] report_path={report_path}")
    log_lines.append(f"[cuda] python_source={py_source}")
    if py_warn:
        log_lines.append(f"[cuda] python_warning={py_warn}")

    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "cuda",
        "task": "check",
        "command": "",
        "timeout_sec": int(args.timeout_sec),
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "observed": {
            "python": py,
            "framework": "unknown",
            "cuda_available": False,
            "gpu_count": 0,
        },
        "meta": {
            "python_resolution": {"source": py_source, "warning": py_warn, "report_path": str(report_path)},
            "timestamp_utc": _utc_now_iso(),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    if py is None:
        results["failure_category"] = "missing_report"
        results["error_excerpt"] = f"Could not resolve python: {py_warn or ''}"
        log_lines.append(f"[cuda] ERROR: {results['error_excerpt']}")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        _write_json(out_dir / "results.json", results)
        return 1

    cmd = [py, "-c", PROBE_CODE]
    results["command"] = _quote_cmd(cmd)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=int(args.timeout_sec),
        )
    except subprocess.TimeoutExpired as e:
        log_lines.append("[cuda] ERROR: timeout")
        results["failure_category"] = "timeout"
        results["error_excerpt"] = str(e)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        _write_json(out_dir / "results.json", results)
        return 1
    except Exception as e:
        log_lines.append(f"[cuda] ERROR: {e}")
        results["failure_category"] = "runtime"
        results["error_excerpt"] = str(e)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        _write_json(out_dir / "results.json", results)
        return 1

    log_lines.append(f"[cuda] probe_returncode={proc.returncode}")
    if proc.stdout:
        log_lines.append("[cuda] --- probe stdout ---")
        log_lines.append(proc.stdout.rstrip("\n"))
    if proc.stderr:
        log_lines.append("[cuda] --- probe stderr ---")
        log_lines.append(proc.stderr.rstrip("\n"))

    probe_raw = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
    try:
        probe = json.loads(probe_raw) if probe_raw else {}
    except Exception as e:
        results["failure_category"] = "invalid_json"
        results["error_excerpt"] = _tail_lines("\n".join(log_lines) + f"\nparse_error={e}")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        _write_json(out_dir / "results.json", results)
        return 1

    framework = str(probe.get("framework", "unknown"))
    results["framework"] = framework if framework in ("pytorch", "tensorflow", "jax") else "unknown"
    results["observed"]["framework"] = results["framework"]

    cuda_available = False
    gpu_count = 0
    if results["framework"] == "pytorch":
        cuda_available = bool(probe.get("torch", {}).get("cuda_available", False))
        gpu_count = int(probe.get("torch", {}).get("gpu_count", 0) or 0)
    elif results["framework"] == "tensorflow":
        cuda_available = bool(probe.get("tensorflow", {}).get("cuda_available", False))
        gpu_count = int(probe.get("tensorflow", {}).get("gpu_count", 0) or 0)
    elif results["framework"] == "jax":
        cuda_available = bool(probe.get("jax", {}).get("cuda_available", False))
        gpu_count = int(probe.get("jax", {}).get("gpu_count", 0) or 0)

    results["observed"]["cuda_available"] = cuda_available
    results["observed"]["gpu_count"] = gpu_count
    results["meta"]["probe"] = probe

    if results["framework"] == "unknown":
        results["failure_category"] = "deps"
        results["error_excerpt"] = _tail_lines("\n".join(log_lines))
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        _write_json(out_dir / "results.json", results)
        return 1

    if cuda_available:
        results["status"] = "success"
        results["exit_code"] = 0
        results["failure_category"] = "unknown"
        results["error_excerpt"] = ""
        log_lines.append(f"[cuda] cuda_available=true gpu_count={gpu_count}")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        _write_json(out_dir / "results.json", results)
        return 0

    results["status"] = "failure"
    results["exit_code"] = 1
    results["failure_category"] = "insufficient_hardware"
    results["error_excerpt"] = _tail_lines("\n".join(log_lines))
    log_lines.append("[cuda] cuda_available=false")
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    _write_json(out_dir / "results.json", results)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

