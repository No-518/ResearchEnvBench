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
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_report_path(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception:
        return None, "missing_report"
    try:
        data = json.loads(raw)
    except Exception:
        return None, "invalid_json"
    if not isinstance(data, dict):
        return None, "invalid_json"
    return data, None


def tail_text(path: Path, n: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except Exception:
        return ""


def cmd_str(cmd: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


CHECK_SNIPPET = r"""
import json
out = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "details": {}}

def _emit():
    print(json.dumps(out, ensure_ascii=False))

try:
    import torch  # noqa: F401
    import torch
    out["framework"] = "pytorch"
    out["details"]["torch_version"] = getattr(torch, "__version__", "")
    out["cuda_available"] = bool(torch.cuda.is_available())
    out["gpu_count"] = int(torch.cuda.device_count())
    _emit()
    raise SystemExit(0)
except SystemExit:
    raise
except Exception as e:
    out["details"]["torch_import_error"] = str(e)

try:
    import tensorflow as tf  # noqa: F401
    import tensorflow as tf
    out["framework"] = "tensorflow"
    out["details"]["tf_version"] = getattr(tf, "__version__", "")
    gpus = tf.config.list_physical_devices("GPU")
    out["gpu_count"] = int(len(gpus))
    out["cuda_available"] = out["gpu_count"] > 0
    _emit()
    raise SystemExit(0)
except SystemExit:
    raise
except Exception as e:
    out["details"]["tf_import_error"] = str(e)

try:
    import jax  # noqa: F401
    import jax
    out["framework"] = "jax"
    devices = jax.devices()
    gpu_devices = [d for d in devices if getattr(d, "platform", "") == "gpu"]
    out["gpu_count"] = int(len(gpu_devices))
    out["cuda_available"] = out["gpu_count"] > 0
    out["details"]["jax_version"] = getattr(jax, "__version__", "")
    _emit()
    raise SystemExit(0)
except SystemExit:
    raise
except Exception as e:
    out["details"]["jax_import_error"] = str(e)

_emit()
raise SystemExit(0)
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check CUDA availability (torch/tf/jax) using report python.")
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python", default=None, help="Override python executable (else: report.json python_path).")
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(msg: str) -> None:
        log_path.write_text(log_path.read_text(encoding="utf-8", errors="replace") + msg + "\n" if log_path.exists() else msg + "\n", encoding="utf-8")
        print(msg)

    # Ensure log file exists
    log_path.write_text("", encoding="utf-8")
    log(f"[cuda] start_utc={utc_now_iso()}")
    log(f"[cuda] repo_root={root}")

    report_path = resolve_report_path(args.report_path)
    reported: dict[str, Any] | None = None

    python_exe: str | None = None
    failure_category: str = "unknown"
    status: str = "failure"
    exit_code: int = 1
    framework: str = "unknown"
    observed: dict[str, Any] = {}

    if args.python:
        python_exe = args.python
        log(f"[cuda] python_source=cli --python {python_exe}")
    else:
        report, err = read_json(report_path)
        if err is not None:
            failure_category = "missing_report" if err == "missing_report" else "invalid_json"
            log(f"[cuda] ERROR: report {failure_category}: {report_path}")
            payload = {
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
                    "python": "",
                    "git_commit": "",
                    "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
                    "decision_reason": "Resolve python via report.json python_path, then probe CUDA availability.",
                    "timestamp_utc": utc_now_iso(),
                },
                "observed": {},
                "failure_category": failure_category,
                "error_excerpt": tail_text(log_path),
            }
            results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 1
        reported = report
        python_exe = str((report.get("python_path") or "")).strip()
        log(f"[cuda] python_source=report:python_path {python_exe}")

    if not python_exe or not Path(python_exe).exists() or not os.access(python_exe, os.X_OK):
        failure_category = "path_hallucination"
        log(f"[cuda] ERROR: python not executable: {python_exe}")
        payload = {
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
                "python": python_exe or "",
                "git_commit": "",
                "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
                "decision_reason": "Resolve python via report.json python_path, then probe CUDA availability.",
                "timestamp_utc": utc_now_iso(),
            },
            "observed": {},
            "failure_category": failure_category,
            "error_excerpt": tail_text(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    cmd = [python_exe, "-c", CHECK_SNIPPET]
    log(f"[cuda] cmd={cmd_str(cmd)}")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        failure_category = "timeout"
        log("[cuda] ERROR: timeout running CUDA probe")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "cuda",
            "task": "check",
            "command": cmd_str(cmd),
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "meta": {
                "python": python_exe,
                "git_commit": "",
                "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "")},
                "decision_reason": "Probe CUDA via importing torch/tf/jax in the reported python.",
                "timestamp_utc": utc_now_iso(),
            },
            "observed": {},
            "failure_category": failure_category,
            "error_excerpt": tail_text(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if stderr:
        log("[cuda] stderr:\n" + stderr)
    log("[cuda] stdout:\n" + stdout)

    try:
        obs = json.loads(stdout) if stdout else {}
    except Exception:
        obs = {}

    framework = str(obs.get("framework", "unknown"))
    cuda_available = bool(obs.get("cuda_available", False))
    gpu_count = int(obs.get("gpu_count", 0) or 0)
    observed = {"cuda_available": cuda_available, "gpu_count": gpu_count, "framework": framework, "details": obs.get("details", {})}

    if framework == "unknown":
        status = "failure"
        exit_code = 1
        failure_category = "deps"
    elif cuda_available and gpu_count > 0:
        status = "success"
        exit_code = 0
        failure_category = "unknown"
    else:
        status = "failure"
        exit_code = 1
        failure_category = "unknown"

    payload = {
        "status": status,
        "skip_reason": "not_applicable" if status == "success" else "unknown",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": cmd_str(cmd),
        "timeout_sec": 120,
        "framework": framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": python_exe,
            "git_commit": "",
            "env_vars": {"CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "")},
            "decision_reason": "Probe CUDA via importing torch/tf/jax in the reported python.",
            "timestamp_utc": utc_now_iso(),
            "report_path": str(report_path),
            "reported": reported or {},
        },
        "observed": observed,
        "failure_category": failure_category,
        "error_excerpt": tail_text(log_path),
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

