#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tail_text(path: Path, max_lines: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            dq: deque[str] = deque(f, maxlen=max_lines)
        return "".join(dq).strip()
    except Exception:
        return ""


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def resolve_python(cli_python: str | None, report_path: Path) -> tuple[str | None, str | None]:
    if cli_python:
        return cli_python, None
    env = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env:
        return env, None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"missing report: {report_path}"
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid report json: {report_path}: {exc}"
    py = report.get("python_path")
    if not isinstance(py, str) or not py.strip():
        return None, f"report missing python_path: {report_path}"
    return py, None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None)
    ap.add_argument("--python", default=None, help="Override python interpreter to check")
    args = ap.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    stage_dir = repo_root / "build_output" / "cuda"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    started_utc = utc_now()
    report_path = resolve_report_path(args.report_path)
    python_bin, py_err = resolve_python(args.python, report_path)

    status = "failure"
    failure_category = "missing_report" if py_err else "unknown"
    error_excerpt = ""
    framework = "unknown"
    observed: dict[str, Any] = {
        "cuda_available": False,
        "gpu_count": 0,
        "torch": {"available": False},
        "tensorflow": {"available": False},
        "jax": {"available": False},
        "used_framework": "unknown",
    }

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[cuda] started_utc={started_utc}\n")
        log.write(f"[cuda] report_path={report_path}\n")
        if py_err:
            log.write(f"[cuda] ERROR: {py_err}\n")
            error_excerpt = py_err
        else:
            log.write(f"[cuda] python={python_bin}\n")

            snippet = textwrap.dedent(
                """
                import json

                out = {
                    "torch": {"available": False},
                    "tensorflow": {"available": False},
                    "jax": {"available": False},
                    "used_framework": "unknown",
                    "cuda_available": False,
                    "gpu_count": 0,
                }

                try:
                    import torch
                    out["torch"]["available"] = True
                    out["torch"]["cuda_available"] = bool(torch.cuda.is_available())
                    out["torch"]["gpu_count"] = int(torch.cuda.device_count())
                except Exception as e:
                    out["torch"]["error"] = str(e)

                try:
                    import tensorflow as tf
                    out["tensorflow"]["available"] = True
                    gpus = tf.config.list_physical_devices("GPU")
                    out["tensorflow"]["gpu_count"] = len(gpus)
                    out["tensorflow"]["cuda_available"] = len(gpus) > 0
                except Exception as e:
                    out["tensorflow"]["error"] = str(e)

                try:
                    import jax
                    out["jax"]["available"] = True
                    devices = jax.devices()
                    gpu_devices = [d for d in devices if getattr(d, "platform", "") == "gpu"]
                    out["jax"]["gpu_count"] = len(gpu_devices)
                    out["jax"]["cuda_available"] = len(gpu_devices) > 0
                except Exception as e:
                    out["jax"]["error"] = str(e)

                # Choose primary framework.
                if out["torch"].get("available"):
                    out["used_framework"] = "pytorch"
                    out["cuda_available"] = bool(out["torch"].get("cuda_available", False))
                    out["gpu_count"] = int(out["torch"].get("gpu_count", 0))
                elif out["tensorflow"].get("available"):
                    out["used_framework"] = "tensorflow"
                    out["cuda_available"] = bool(out["tensorflow"].get("cuda_available", False))
                    out["gpu_count"] = int(out["tensorflow"].get("gpu_count", 0))
                elif out["jax"].get("available"):
                    out["used_framework"] = "jax"
                    out["cuda_available"] = bool(out["jax"].get("cuda_available", False))
                    out["gpu_count"] = int(out["jax"].get("gpu_count", 0))

                print(json.dumps(out))
                """
            ).strip()

            try:
                proc = subprocess.run(
                    [python_bin, "-c", snippet],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                log.write(proc.stdout)
                if proc.stderr:
                    log.write("\n[cuda] stderr:\n")
                    log.write(proc.stderr)
                if proc.returncode != 0:
                    failure_category = "runtime"
                    error_excerpt = f"python returned {proc.returncode}"
                else:
                    observed = json.loads(proc.stdout.strip() or "{}")
                    framework = observed.get("used_framework", "unknown")
                    if observed.get("cuda_available"):
                        status = "success"
                        failure_category = ""
                    else:
                        status = "failure"
                        failure_category = "runtime"
            except subprocess.TimeoutExpired:
                status = "failure"
                failure_category = "timeout"
                error_excerpt = "cuda check timed out"
            except Exception as exc:  # noqa: BLE001
                status = "failure"
                failure_category = "runtime"
                error_excerpt = str(exc)

        finished_utc = utc_now()
        log.write(f"\n[cuda] finished_utc={finished_utc}\n")

    exit_code = 0 if status == "success" else 1
    if not error_excerpt and status == "failure":
        error_excerpt = tail_text(log_path, 220)

    payload: dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": f"{python_bin} -c <cuda_check_snippet>" if python_bin else "report/python resolution",
        "timeout_sec": 120,
        "framework": framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "observed": observed,
        "meta": {
            "reported_python_path": python_bin,
            "report_path": str(report_path),
            "started_utc": started_utc,
            "finished_utc": utc_now(),
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt[-4000:] if error_excerpt else "",
    }
    write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

