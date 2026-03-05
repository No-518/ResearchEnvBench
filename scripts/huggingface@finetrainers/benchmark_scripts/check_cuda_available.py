#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-max_lines:])
    except Exception:
        return ""


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, f"Expected JSON object in {path}"
        return data, None
    except FileNotFoundError:
        return None, f"Missing report: {path}"
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON in {path}: {e}"
    except Exception as e:
        return None, f"Failed to read {path}: {e}"


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return DEFAULT_REPORT_PATH


def git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Check CUDA availability using the python from agent report.")
    parser.add_argument("--report-path", type=str, default=None)
    parser.add_argument("--out-root", type=str, default="build_output")
    args = parser.parse_args()

    out_root = (REPO_ROOT / args.out_root).resolve()
    stage_dir = out_root / "cuda"
    ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report, report_err = read_json(report_path)

    base: Dict[str, Any] = {
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
            "git_commit": git_commit(),
            "env_vars": {
                k: os.environ.get(k)
                for k in ["SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON", "CUDA_VISIBLE_DEVICES"]
                if os.environ.get(k) is not None
            },
            "decision_reason": "Check CUDA availability in the environment specified by agent report python_path.",
            "timestamp_utc": utc_now_iso(),
            "report_path": str(report_path),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    if report is None:
        log_path.write_text(f"[{utc_now_iso()}] {report_err}\n", encoding="utf-8")
        base["failure_category"] = "missing_report"
        base["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    python_path = report.get("python_path")
    if not python_path:
        log_path.write_text(f"[{utc_now_iso()}] report missing python_path\n", encoding="utf-8")
        base["failure_category"] = "missing_report"
        base["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    base["meta"]["python"] = str(python_path)
    base["command"] = f"{python_path} -c <cuda_check_snippet>"

    snippet = r"""
import json
out = {"framework":"unknown","cuda_available": False, "gpu_count": 0, "details": {}}
try:
    import torch
    out["framework"] = "pytorch"
    out["cuda_available"] = bool(torch.cuda.is_available())
    out["gpu_count"] = int(torch.cuda.device_count())
    out["details"]["torch_version"] = getattr(torch, "__version__", "")
except Exception as e:
    out["details"]["torch_error"] = str(e)
    try:
        import tensorflow as tf
        out["framework"] = "tensorflow"
        gpus = tf.config.list_physical_devices("GPU")
        out["cuda_available"] = bool(gpus)
        out["gpu_count"] = int(len(gpus))
        out["details"]["tf_version"] = getattr(tf, "__version__", "")
    except Exception as e2:
        out["details"]["tf_error"] = str(e2)
        try:
            import jax
            out["framework"] = "jax"
            devs = [d for d in jax.devices() if d.platform == "gpu"]
            out["cuda_available"] = bool(devs)
            out["gpu_count"] = int(len(devs))
            out["details"]["jax_version"] = getattr(jax, "__version__", "")
        except Exception as e3:
            out["details"]["jax_error"] = str(e3)
print(json.dumps(out))
"""

    try:
        proc = subprocess.run(
            [str(python_path), "-c", snippet],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as e:
        log_path.write_text(f"[{utc_now_iso()}] failed to run cuda check: {e}\n", encoding="utf-8")
        base["failure_category"] = "runtime"
        base["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    log_path.write_text(
        f"[{utc_now_iso()}] python_path={python_path}\n"
        f"[{utc_now_iso()}] returncode={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}\n",
        encoding="utf-8",
    )

    observed: Dict[str, Any] = {"framework": "unknown", "cuda_available": False, "gpu_count": 0}
    try:
        observed = json.loads(proc.stdout.strip() or "{}")
    except Exception:
        observed = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "details": {"parse_error": True}}

    base["framework"] = observed.get("framework") or "unknown"
    base["observed"] = {
        "cuda_available": bool(observed.get("cuda_available")),
        "gpu_count": int(observed.get("gpu_count") or 0),
        "details": observed.get("details") or {},
    }

    cuda_ok = bool(base["observed"]["cuda_available"]) and int(base["observed"]["gpu_count"]) > 0 and proc.returncode == 0
    if cuda_ok:
        base["status"] = "success"
        base["exit_code"] = 0
        base["failure_category"] = ""
        base["error_excerpt"] = ""
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    base["status"] = "failure"
    base["exit_code"] = 1
    base["failure_category"] = "insufficient_hardware"
    base["error_excerpt"] = tail_text(log_path)
    results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

