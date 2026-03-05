#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: pathlib.Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tail_text(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)


def resolve_report_path(cli_path: str | None) -> pathlib.Path:
    if cli_path:
        return pathlib.Path(cli_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return pathlib.Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return pathlib.Path(DEFAULT_REPORT_PATH)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Check CUDA availability via torch/tensorflow/jax.")
    ap.add_argument("--report-path", default=None, help="Override report.json path.")
    ap.add_argument("--timeout-sec", type=int, default=60, help="Timeout for the probe subprocess.")
    args = ap.parse_args(argv)

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    out_dir = repo_root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)

    status = "failure"
    exit_code = 1
    failure_category = "unknown"
    framework = "unknown"
    observed: dict[str, Any] = {}
    command = ""

    with log_path.open("w", encoding="utf-8") as log_fp:
        log_fp.write(f"[cuda] time_utc={now_utc_iso()}\n")
        log_fp.write(f"[cuda] report_path={report_path}\n")

        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            failure_category = "missing_report"
            log_fp.write("[cuda] report missing\n")
            report = None
        except Exception as e:
            failure_category = "invalid_json"
            log_fp.write(f"[cuda] report parse failed: {type(e).__name__}: {e}\n")
            report = None

        python_path = ""
        if isinstance(report, dict):
            python_path_val = report.get("python_path")
            if isinstance(python_path_val, str):
                python_path = python_path_val

        if not python_path:
            failure_category = "missing_report"
            log_fp.write("[cuda] python_path missing in report\n")
        elif not (os.path.isfile(python_path) and os.access(python_path, os.X_OK)):
            failure_category = "path_hallucination"
            log_fp.write(f"[cuda] python_path not executable: {python_path!r}\n")
        else:
            probe_code = r"""
import json
out = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "details": {}}

def try_torch():
    try:
        import torch
        out["framework"] = "pytorch"
        out["details"]["torch_version"] = getattr(torch, "__version__", "")
        out["cuda_available"] = bool(torch.cuda.is_available())
        out["gpu_count"] = int(torch.cuda.device_count()) if out["cuda_available"] else 0
        return True
    except Exception as e:
        out["details"]["torch_error"] = f"{type(e).__name__}: {e}"
        return False

def try_tf():
    try:
        import tensorflow as tf
        out["framework"] = "tensorflow"
        out["details"]["tf_version"] = getattr(tf, "__version__", "")
        gpus = tf.config.list_physical_devices("GPU")
        out["cuda_available"] = bool(gpus)
        out["gpu_count"] = len(gpus)
        return True
    except Exception as e:
        out["details"]["tf_error"] = f"{type(e).__name__}: {e}"
        return False

def try_jax():
    try:
        import jax
        out["framework"] = "jax"
        out["details"]["jax_version"] = getattr(jax, "__version__", "")
        backend = getattr(jax, "default_backend", lambda: "unknown")()
        out["details"]["jax_backend"] = backend
        devices = list(getattr(jax, "devices", lambda: [])())
        out["cuda_available"] = any(getattr(d, "platform", "") == "gpu" for d in devices) or backend == "gpu"
        out["gpu_count"] = sum(1 for d in devices if getattr(d, "platform", "") == "gpu")
        return True
    except Exception as e:
        out["details"]["jax_error"] = f"{type(e).__name__}: {e}"
        return False

ok = try_torch() or try_tf() or try_jax()
print(json.dumps(out))
"""
            command = f"{python_path} -c <cuda_probe>"
            try:
                proc = subprocess.run(
                    [python_path, "-c", probe_code],
                    capture_output=True,
                    text=True,
                    timeout=args.timeout_sec,
                    cwd=repo_root,
                    env=os.environ.copy(),
                )
            except subprocess.TimeoutExpired:
                failure_category = "timeout"
                log_fp.write(f"[cuda] probe timeout after {args.timeout_sec}s\n")
            else:
                log_fp.write(f"[cuda] probe_returncode={proc.returncode}\n")
                if proc.stdout:
                    log_fp.write("[cuda] probe_stdout:\n")
                    log_fp.write(proc.stdout + ("\n" if not proc.stdout.endswith("\n") else ""))
                if proc.stderr:
                    log_fp.write("[cuda] probe_stderr:\n")
                    log_fp.write(proc.stderr + ("\n" if not proc.stderr.endswith("\n") else ""))

                try:
                    observed = json.loads(proc.stdout.strip() or "{}")
                except Exception as e:
                    failure_category = "invalid_json"
                    log_fp.write(f"[cuda] failed to parse probe JSON: {type(e).__name__}: {e}\n")
                else:
                    framework = str(observed.get("framework", "unknown"))
                    cuda_available = bool(observed.get("cuda_available", False))
                    gpu_count = int(observed.get("gpu_count", 0) or 0)
                    observed["cuda_available"] = cuda_available
                    observed["gpu_count"] = gpu_count

                    if cuda_available and gpu_count > 0:
                        status = "success"
                        exit_code = 0
                        failure_category = ""
                    else:
                        status = "failure"
                        exit_code = 1
                        failure_category = "runtime"

    result = {
        "status": status,
        "skip_reason": "not_applicable" if status == "success" else "unknown",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": command,
        "timeout_sec": args.timeout_sec,
        "framework": framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "reported_python_path": (report or {}).get("python_path") if isinstance(report, dict) else "",
            "timestamp_utc": now_utc_iso(),
        },
        "observed": observed,
        "failure_category": failure_category,
        "error_excerpt": tail_text(log_path),
    }
    write_json(results_path, result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

