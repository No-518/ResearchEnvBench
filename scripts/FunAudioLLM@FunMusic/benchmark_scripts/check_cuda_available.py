#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _resolve_report_path(cli_path: Optional[str]) -> Path:
    if cli_path:
        return Path(cli_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def _resolve_python(report_path: Path) -> Dict[str, Any]:
    python_override = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if python_override:
        return {"python": python_override, "source": "env"}
    report = _read_json(report_path)
    if report and isinstance(report.get("python_path"), str):
        return {"python": report["python_path"], "source": "report.json", "reported": report}
    return {"python": sys.executable, "source": "sys.executable", "reported": report}


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check CUDA availability using the agent-reported python environment.")
    parser.add_argument("--report-path", help="Override report path (else SCIMLOPSBENCH_REPORT or default).")
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)
    py = _resolve_python(report_path)
    python_exe = py["python"]

    check_code = textwrap.dedent(
        r"""
        import json
        observed = {"framework": "unknown", "cuda_available": False, "gpu_count": 0}

        try:
            import torch
            observed["framework"] = "pytorch"
            observed["cuda_available"] = bool(torch.cuda.is_available())
            observed["gpu_count"] = int(torch.cuda.device_count())
            print(json.dumps(observed))
            raise SystemExit(0)
        except Exception:
            pass

        try:
            import tensorflow as tf
            observed["framework"] = "tensorflow"
            gpus = tf.config.list_physical_devices("GPU")
            observed["gpu_count"] = int(len(gpus))
            observed["cuda_available"] = observed["gpu_count"] > 0
            print(json.dumps(observed))
            raise SystemExit(0)
        except Exception:
            pass

        try:
            import jax
            observed["framework"] = "jax"
            devs = jax.devices()
            observed["gpu_count"] = int(sum(1 for d in devs if getattr(d, "platform", "") == "gpu"))
            observed["cuda_available"] = observed["gpu_count"] > 0
            print(json.dumps(observed))
            raise SystemExit(0)
        except Exception:
            pass

        print(json.dumps(observed))
        """
    ).strip()

    cmd = [python_exe, "-c", check_code]
    cmd_str = " ".join(subprocess.list2cmdline([c]) if " " in c else c for c in cmd)

    proc = None
    stdout = ""
    stderr = ""
    rc = 1
    observed: Dict[str, Any] = {"framework": "unknown", "cuda_available": False, "gpu_count": 0}

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        rc = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        try:
            observed = json.loads(stdout.strip().splitlines()[-1])
        except Exception:
            observed = {"framework": "unknown", "cuda_available": False, "gpu_count": 0}
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or "") if isinstance(e.stdout, str) else ""
        stderr = (e.stderr or "") if isinstance(e.stderr, str) else ""
        rc = 1
    except Exception as e:
        stderr = f"{type(e).__name__}: {e}\n"
        rc = 1

    log_text = []
    log_text.append(f"[cuda] timestamp_utc={_utc_now_iso()}")
    log_text.append(f"[cuda] report_path={report_path}")
    log_text.append(f"[cuda] python_exe({py.get('source')})={python_exe}")
    log_text.append(f"[cuda] cmd={cmd_str}")
    log_text.append("")
    if stderr.strip():
        log_text.append("=== stderr ===")
        log_text.append(stderr.rstrip())
    if stdout.strip():
        log_text.append("=== stdout ===")
        log_text.append(stdout.rstrip())
    log_text.append("")
    log_text.append(f"[cuda] observed={observed}")
    log_path.write_text("\n".join(log_text) + "\n", encoding="utf-8")

    cuda_available = bool(observed.get("cuda_available"))
    exit_code = 0 if cuda_available else 1
    status = "success" if cuda_available else "failure"

    failure_category = "unknown"
    error_excerpt = ""
    if exit_code != 0:
        failure_category = "runtime"
        error_excerpt = "\n".join((stderr + "\n" + stdout).splitlines()[-220:])

    results = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": f"python benchmark_scripts/check_cuda_available.py (subprocess: {cmd_str})",
        "timeout_sec": 120,
        "framework": observed.get("framework", "unknown"),
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": python_exe,
            "python_resolution": py.get("source"),
            "git_commit": None,
            "report_path": str(report_path),
            "timestamp_utc": _utc_now_iso(),
        },
        "observed": {
            "cuda_available": cuda_available,
            "gpu_count": int(observed.get("gpu_count") or 0),
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    # Best-effort git commit
    try:
        results["meta"]["git_commit"] = (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True).strip()
        )
    except Exception:
        results["meta"]["git_commit"] = None

    _write_json(results_path, results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

