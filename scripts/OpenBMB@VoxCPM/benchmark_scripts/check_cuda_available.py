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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def get_git_commit(root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def load_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"Missing JSON file: {path}"
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON in {path}: {e}"
    except Exception as e:
        return None, f"Failed to read {path}: {e}"


def report_path(cli: Optional[str]) -> Path:
    if cli:
        return Path(cli)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path("/opt/scimlopsbench/report.json")


def resolve_python(cli_python: Optional[str], rpt_path: Path) -> Tuple[Optional[str], str, Optional[str]]:
    if cli_python:
        return cli_python, "cli", None
    if os.environ.get("SCIMLOPSBENCH_PYTHON"):
        return os.environ["SCIMLOPSBENCH_PYTHON"], "env:SCIMLOPSBENCH_PYTHON", None
    rpt, err = load_json(rpt_path)
    if rpt is None:
        return None, "missing_report", err
    py = rpt.get("python_path") if isinstance(rpt, dict) else None
    if isinstance(py, str) and py:
        return py, "report:python_path", None
    return sys.executable, "fallback:sys.executable", "report.json valid but missing python_path; falling back to current interpreter"


CHECK_SNIPPET = r"""
import json
import platform

out = {
  "framework": "unknown",
  "python_version": platform.python_version(),
  "python_executable": None,
  "torch_import_ok": False,
  "torch_version": "",
  "cuda_available": False,
  "gpu_count": 0,
  "gpu_names": [],
  "errors": [],
}

try:
  import sys
  out["python_executable"] = sys.executable
except Exception:
  pass

def cap_gpu_names(names, cap=8):
  return names[:cap]

try:
  import torch
  out["framework"] = "pytorch"
  out["torch_import_ok"] = True
  out["torch_version"] = getattr(torch, "__version__", "") or ""
  try:
    out["cuda_available"] = bool(torch.cuda.is_available())
    out["gpu_count"] = int(torch.cuda.device_count())
    names = []
    for i in range(min(out["gpu_count"], 8)):
      try:
        names.append(torch.cuda.get_device_name(i))
      except Exception:
        names.append("")
    out["gpu_names"] = cap_gpu_names(names)
  except Exception as e:
    out["errors"].append(f"torch_cuda_check_failed: {e}")
except Exception as e:
  out["errors"].append(f"torch_import_failed: {e}")

if out["framework"] == "unknown":
  try:
    import tensorflow as tf
    out["framework"] = "tensorflow"
    try:
      gpus = tf.config.list_physical_devices("GPU")
      out["gpu_count"] = int(len(gpus))
      out["cuda_available"] = out["gpu_count"] > 0
      out["gpu_names"] = cap_gpu_names([getattr(g, "name", "") for g in gpus])
    except Exception as e:
      out["errors"].append(f"tf_gpu_check_failed: {e}")
  except Exception as e:
    out["errors"].append(f"tf_import_failed: {e}")

if out["framework"] == "unknown":
  try:
    import jax
    out["framework"] = "jax"
    try:
      devs = jax.devices()
      gpus = [d for d in devs if getattr(d, "platform", "") == "gpu"]
      out["gpu_count"] = int(len(gpus))
      out["cuda_available"] = out["gpu_count"] > 0
      out["gpu_names"] = cap_gpu_names([str(d) for d in gpus])
    except Exception as e:
      out["errors"].append(f"jax_gpu_check_failed: {e}")
  except Exception as e:
    out["errors"].append(f"jax_import_failed: {e}")

print(json.dumps(out))
"""


def tail_lines(path: Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python", default=None)
    parser.add_argument("--timeout-sec", type=int, default=120)
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "cuda"
    safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    rpt_path = report_path(args.report_path)
    py, py_source, py_warn = resolve_python(args.python, rpt_path)

    status = "failure"
    skip_reason = "not_applicable"
    exit_code = 1
    failure_category = "unknown"
    framework = "unknown"
    observed: Dict[str, Any] = {"cuda_available": False, "gpu_count": 0}

    cmd_str = ""
    if not py:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"ERROR: missing or invalid report.json at {rpt_path}; cannot resolve python.\n")
            if py_warn:
                f.write(py_warn + "\n")
        failure_category = "missing_report"
    else:
        cmd = [py, "-c", CHECK_SNIPPET]
        cmd_str = " ".join([subprocess.list2cmdline([c]) if " " in c else c for c in cmd])
        try:
            p = subprocess.run(
                cmd,
                cwd=str(root),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=args.timeout_sec,
                check=False,
                text=True,
            )
            log_path.write_text(p.stdout or "", encoding="utf-8")
            if p.returncode != 0:
                failure_category = "runtime"
            else:
                try:
                    obs = json.loads((p.stdout or "").splitlines()[-1])
                    framework = str(obs.get("framework", "unknown"))
                    observed = {
                        "python_executable": obs.get("python_executable") or py,
                        "python_version": obs.get("python_version", ""),
                        "framework": framework,
                        "torch_import_ok": bool(obs.get("torch_import_ok", False)),
                        "torch_version": obs.get("torch_version", ""),
                        "cuda_available": bool(obs.get("cuda_available", False)),
                        "gpu_count": int(obs.get("gpu_count", 0) or 0),
                        "gpu_names": obs.get("gpu_names", []),
                        "errors": obs.get("errors", []),
                        "python_source": py_source,
                    }
                    if py_warn:
                        observed["warnings"] = [py_warn]
                except Exception as e:
                    failure_category = "invalid_json"
                    with log_path.open("a", encoding="utf-8") as f:
                        f.write(f"\nERROR: failed to parse JSON from check snippet: {e}\n")
                else:
                    # Stage semantics: CUDA unavailable is a failing check (exit 1).
                    if observed.get("cuda_available"):
                        status = "success"
                        exit_code = 0
                        failure_category = "unknown"
                    else:
                        status = "failure"
                        exit_code = 1
                        failure_category = "runtime"
        except subprocess.TimeoutExpired:
            with log_path.open("a", encoding="utf-8") as f:
                f.write("\nERROR: cuda check timed out\n")
            failure_category = "timeout"
        except FileNotFoundError:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"ERROR: python executable not found: {py}\n")
            failure_category = "path_hallucination"
        except Exception as e:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"ERROR: unexpected error: {e}\n")
            failure_category = "unknown"

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": cmd_str,
        "timeout_sec": int(args.timeout_sec),
        "framework": framework if framework in ("pytorch", "tensorflow", "jax") else "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "observed": observed,
        "meta": {
            "python": sys.executable,
            "git_commit": get_git_commit(root),
            "timestamp_utc": utc_now_iso(),
            "env_vars": {k: os.environ.get(k, "") for k in ["CUDA_VISIBLE_DEVICES", "SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON"]},
        },
        "failure_category": failure_category if status == "failure" else "unknown",
        "error_excerpt": tail_lines(log_path),
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

