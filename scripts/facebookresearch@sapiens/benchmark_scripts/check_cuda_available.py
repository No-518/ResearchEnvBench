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


def _now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_last_lines(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= max_lines:
                step = block if size >= block else size
                size -= step
                f.seek(size, os.SEEK_SET)
                data = f.read(step) + data
            lines = data.splitlines()[-max_lines:]
            return "\n".join(l.decode("utf-8", errors="replace") for l in lines)
    except Exception:
        return ""


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT", "")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def _base_assets() -> dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _git_commit(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check CUDA availability for the benchmarked environment.")
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--timeout-sec", type=int, default=120)
    ns = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "cuda"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stage = "cuda"
    task = "check"
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
    timeout_sec = int(ns.timeout_sec)

    status = "failure"
    exit_code = 1
    failure_category = "unknown"
    observed: dict[str, Any] = {
        "framework": "unknown",
        "cuda_available": False,
        "gpu_count": 0,
        "details": {},
    }

    try:
        report_path = _resolve_report_path(ns.report_path)
        with log_path.open("w", encoding="utf-8") as log_f:
            log_f.write(f"[cuda] report_path={report_path}\n")
            try:
                report = _read_json(report_path)
            except FileNotFoundError:
                failure_category = "missing_report"
                log_f.write("[cuda] missing report.json\n")
                raise
            except json.JSONDecodeError:
                failure_category = "invalid_json"
                log_f.write("[cuda] invalid report.json\n")
                raise

            python_path = str(report.get("python_path") or "")
            if not python_path:
                failure_category = "path_hallucination"
                log_f.write("[cuda] report missing python_path\n")
                raise RuntimeError("report missing python_path")
            if not (Path(python_path).exists() and os.access(python_path, os.X_OK)):
                failure_category = "path_hallucination"
                log_f.write(f"[cuda] python_path not executable: {python_path}\n")
                raise RuntimeError("python_path not executable")

            probe_code = r"""
import json
out = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "details": {}}

def _try_torch():
    import torch
    out["framework"] = "pytorch"
    out["details"]["torch_version"] = getattr(torch, "__version__", "")
    out["cuda_available"] = bool(torch.cuda.is_available())
    out["gpu_count"] = int(torch.cuda.device_count()) if out["cuda_available"] else 0

def _try_tf():
    import tensorflow as tf
    out["framework"] = "tensorflow"
    gpus = tf.config.list_physical_devices("GPU")
    out["details"]["tf_version"] = getattr(tf, "__version__", "")
    out["cuda_available"] = bool(gpus)
    out["gpu_count"] = int(len(gpus))

def _try_jax():
    import jax
    out["framework"] = "jax"
    out["details"]["jax_version"] = getattr(jax, "__version__", "")
    devs = list(jax.devices())
    gpu_devs = [d for d in devs if getattr(d, "platform", "") == "gpu"]
    out["cuda_available"] = bool(gpu_devs)
    out["gpu_count"] = int(len(gpu_devs))

for fn, name in [(_try_torch, "torch"), (_try_tf, "tensorflow"), (_try_jax, "jax")]:
    try:
        fn()
        break
    except Exception as e:
        out["details"][f"{name}_error"] = repr(e)

print(json.dumps(out))
"""
            proc = subprocess.run(
                [python_path, "-c", probe_code],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            log_f.write(f"[cuda] python_exe={python_path}\n")
            log_f.write(f"[cuda] probe_returncode={proc.returncode}\n")
            if proc.stdout:
                log_f.write(proc.stdout + ("\n" if not proc.stdout.endswith("\n") else ""))
            if proc.stderr:
                log_f.write(proc.stderr + ("\n" if not proc.stderr.endswith("\n") else ""))

            if proc.returncode != 0:
                failure_category = "runtime"
                raise RuntimeError("cuda probe subprocess failed")

            payload = json.loads((proc.stdout or "").strip() or "{}")
            observed["framework"] = payload.get("framework", "unknown")
            observed["cuda_available"] = bool(payload.get("cuda_available", False))
            observed["gpu_count"] = int(payload.get("gpu_count", 0) or 0)
            observed["details"] = payload.get("details", {})

            if observed["cuda_available"]:
                status = "success"
                exit_code = 0
                failure_category = ""
            else:
                status = "failure"
                exit_code = 1
                failure_category = "runtime"

    except Exception:
        pass

    results: dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": stage,
        "task": task,
        "command": command,
        "timeout_sec": timeout_sec,
        "framework": observed.get("framework", "unknown") or "unknown",
        "assets": _base_assets(),
        "observed": observed,
        "meta": {
            "python": sys.version.split()[0],
            "git_commit": _git_commit(repo_root),
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
                "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            },
            "decision_reason": "Probe CUDA availability using the agent-reported python_path via torch/tensorflow/jax checks.",
            "timestamp_utc": _now_utc_iso(),
        },
        "failure_category": failure_category or "",
        "error_excerpt": _read_last_lines(log_path, max_lines=240),
    }
    _write_json(results_path, results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
