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
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def safe_env_snapshot() -> dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "PATH",
        "PYTHONPATH",
        "HF_AUTH_TOKEN",
        "HF_TOKEN",
    ]
    out: dict[str, str] = {}
    for k in keys:
        if k not in os.environ:
            continue
        v = os.environ.get(k, "")
        if any(s in k.upper() for s in ("TOKEN", "SECRET", "KEY", "PASS")) and v:
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing_file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid_json: {path}: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"read_error: {path}: {e}"


def git_commit(root: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


CUDA_PROBE_CODE = r"""
import json

out = {
  "framework": "unknown",
  "cuda_available": False,
  "gpu_count": 0,
  "details": {},
  "errors": {},
}

try:
    import torch
    out["framework"] = "pytorch"
    out["details"]["torch_version"] = getattr(torch, "__version__", "")
    out["details"]["torch_cuda_is_available"] = bool(torch.cuda.is_available())
    out["details"]["torch_cuda_device_count"] = int(torch.cuda.device_count() or 0) if torch.cuda.is_available() else 0
    out["cuda_available"] = bool(torch.cuda.is_available())
    out["gpu_count"] = int(out["details"]["torch_cuda_device_count"])
except Exception as e:
    out["errors"]["torch"] = str(e)

if not out["cuda_available"]:
    try:
        import tensorflow as tf
        out["framework"] = "tensorflow"
        out["details"]["tf_version"] = getattr(tf, "__version__", "")
        gpus = tf.config.list_physical_devices("GPU")
        out["gpu_count"] = len(gpus)
        out["cuda_available"] = len(gpus) > 0
    except Exception as e:
        out["errors"]["tensorflow"] = str(e)

if not out["cuda_available"]:
    try:
        import jax
        out["framework"] = "jax"
        out["details"]["jax_version"] = getattr(jax, "__version__", "")
        devs = getattr(jax, "devices", lambda: [])()
        gpus = [d for d in devs if getattr(d, "platform", "") == "gpu"]
        out["gpu_count"] = len(gpus)
        out["cuda_available"] = len(gpus) > 0
    except Exception as e:
        out["errors"]["jax"] = str(e)

print(json.dumps(out))
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check CUDA availability (torch/tensorflow/jax).")
    p.add_argument("--report-path", default=None, help="Override report.json path")
    args = p.parse_args(argv)

    root = repo_root()
    stage_dir = root / "build_output" / "cuda"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    def log(msg: str) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        print(msg)

    report_path = resolve_report_path(args.report_path)
    report, report_err = load_json(report_path)
    python_exe = None
    decision_reason = "Checked CUDA via report python_path."
    warnings: list[str] = []
    if isinstance(report, dict) and isinstance(report.get("python_path"), str) and report.get("python_path", "").strip():
        python_exe = str(report["python_path"]).strip()
    else:
        python_exe = sys.executable
        decision_reason = "Report missing/invalid or python_path missing; checked CUDA via current interpreter."
        if report_err:
            warnings.append(report_err)

    cmd = [python_exe, "-c", CUDA_PROBE_CODE]
    log(f"[{now_utc_iso()}] Running CUDA probe: {shlex.join(cmd)}")

    observed: dict[str, Any] = {
        "python_executable": python_exe,
        "framework": "unknown",
        "cuda_available": False,
        "gpu_count": 0,
    }
    failure_category = "unknown"
    timeout_sec = 600

    try:
        res = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(root),
            env=os.environ.copy(),
        )
        log("[stdout]\n" + (res.stdout or "").strip())
        if res.stderr:
            log("[stderr]\n" + res.stderr.strip())
        try:
            payload = json.loads((res.stdout or "").strip() or "{}")
            if isinstance(payload, dict):
                observed.update(
                    {
                        "framework": payload.get("framework", "unknown"),
                        "cuda_available": bool(payload.get("cuda_available", False)),
                        "gpu_count": int(payload.get("gpu_count", 0) or 0),
                    },
                )
                observed["details"] = payload.get("details", {})
                observed["errors"] = payload.get("errors", {})
        except Exception as e:  # noqa: BLE001
            observed["parse_error"] = str(e)
            failure_category = "invalid_json"
    except subprocess.TimeoutExpired:
        log("CUDA probe timed out.")
        failure_category = "timeout"
    except Exception as e:  # noqa: BLE001
        log(f"CUDA probe failed: {e}")
        failure_category = "runtime"

    cuda_ok = bool(observed.get("cuda_available", False))
    status = "success" if cuda_ok else "failure"
    exit_code = 0 if cuda_ok else 1

    error_excerpt = ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        error_excerpt = "\n".join(lines[-220:])[-8000:]
    except Exception:  # noqa: BLE001
        pass

    results: dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": shlex.join(cmd),
        "timeout_sec": timeout_sec,
        "framework": observed.get("framework", "unknown"),
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": python_exe,
            "git_commit": git_commit(root),
            "env_vars": safe_env_snapshot(),
            "decision_reason": decision_reason,
            "timestamp_utc": now_utc_iso(),
            "report_path": str(report_path),
            "warnings": warnings,
        },
        "observed": {
            "cuda_available": cuda_ok,
            "gpu_count": int(observed.get("gpu_count", 0) or 0),
            "python_executable": python_exe,
            "python_check_exit_code": 0 if python_exe else 1,
            "framework": observed.get("framework", "unknown"),
            "details": observed.get("details", {}),
            "errors": observed.get("errors", {}),
        },
        "failure_category": failure_category if exit_code == 1 else "unknown",
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
