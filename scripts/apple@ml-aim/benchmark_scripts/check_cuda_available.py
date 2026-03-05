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


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _tail_lines(path: Path, *, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def _safe_json_load(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError as e:
        return None, f"invalid_json: {e}"
    except Exception as e:
        return None, f"read_error: {e}"


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
        return out
    except Exception:
        return ""


def _is_executable_file(path: str) -> bool:
    try:
        p = Path(path)
        return p.exists() and p.is_file() and os.access(str(p), os.X_OK)
    except Exception:
        return False


def _resolve_report_path(cli_report_path: Optional[str]) -> str:
    return cli_report_path or os.environ.get("SCIMLOPSBENCH_REPORT") or DEFAULT_REPORT_PATH


def _resolve_python(cli_python: Optional[str], report_path: str) -> Tuple[Optional[str], str, str]:
    if cli_python:
        return cli_python, "cli", ""

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return env_python, "env", ""

    report_file = Path(report_path)
    if not report_file.exists():
        return None, "missing_report", f"missing report.json at {report_path}"

    report_json, report_err = _safe_json_load(report_file)
    if report_err is not None:
        return None, "missing_report", f"invalid report.json at {report_path}: {report_err}"

    python_from_report = (report_json or {}).get("python_path")
    if isinstance(python_from_report, str) and python_from_report.strip():
        return python_from_report.strip(), "report", ""

    return sys.executable, "path_fallback", "report.json missing python_path; fell back to sys.executable"


CUDA_PROBE_CODE = r"""
import json
import subprocess
import sys
import traceback

result = {
  "framework": "unknown",
  "cuda_available": False,
  "gpu_count": 0,
  "details": {},
}

def _set(framework: str, cuda_available: bool, gpu_count: int, details: dict) -> None:
  result["framework"] = framework
  result["cuda_available"] = bool(cuda_available)
  result["gpu_count"] = int(gpu_count)
  result["details"].update(details or {})

try:
  try:
    import torch  # type: ignore

    cuda_avail = bool(torch.cuda.is_available())
    gpu_count = int(torch.cuda.device_count())
    names = []
    if gpu_count > 0:
      try:
        for i in range(gpu_count):
          names.append(str(torch.cuda.get_device_name(i)))
      except Exception:
        pass
    _set(
      "pytorch",
      cuda_avail,
      gpu_count,
      {
        "torch_version": getattr(torch, "__version__", ""),
        "device_names": names,
      },
    )
  except Exception as e_torch:
    result["details"]["torch_import_error"] = repr(e_torch)

  if result["framework"] == "unknown":
    try:
      import tensorflow as tf  # type: ignore

      gpus = tf.config.list_physical_devices("GPU")
      _set(
        "tensorflow",
        len(gpus) > 0,
        len(gpus),
        {"tensorflow_version": getattr(tf, "__version__", ""), "gpu_devices": [d.name for d in gpus]},
      )
    except Exception as e_tf:
      result["details"]["tensorflow_import_error"] = repr(e_tf)

  if result["framework"] == "unknown":
    try:
      import jax  # type: ignore

      devices = list(jax.devices())
      gpu_devices = [d for d in devices if getattr(d, "platform", "") == "gpu"]
      _set(
        "jax",
        len(gpu_devices) > 0,
        len(gpu_devices),
        {"jax_version": getattr(jax, "__version__", ""), "gpu_devices": [str(d) for d in gpu_devices]},
      )
    except Exception as e_jax:
      result["details"]["jax_import_error"] = repr(e_jax)

  if result["framework"] == "unknown":
    try:
      out = subprocess.check_output(
        ["nvidia-smi", "-L"],
        text=True,
        stderr=subprocess.STDOUT,
        timeout=10,
      )
      lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("GPU ")]
      if lines:
        _set("unknown", True, len(lines), {"nvidia_smi": out.strip()})
      else:
        result["details"]["nvidia_smi"] = out.strip()
    except Exception as e_smi:
      result["details"]["nvidia_smi_error"] = repr(e_smi)
except Exception:
  result["details"]["internal_error"] = traceback.format_exc()

print(json.dumps(result, ensure_ascii=False))
sys.exit(0 if result.get("cuda_available") else 1)
""".strip()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Check CUDA availability for the benchmark environment.")
    parser.add_argument("--report-path", default=None, help="Override report.json path.")
    parser.add_argument("--python", default=None, help="Override python executable to probe.")
    parser.add_argument("--timeout-sec", type=int, default=120)
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "cuda"
    _ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    status = "failure"
    exit_code = 1
    skip_reason = "unknown"
    failure_category = "unknown"
    framework = "unknown"
    observed: Dict[str, Any] = {}

    report_path = _resolve_report_path(args.report_path)
    python_exe, py_method, py_warning = _resolve_python(args.python, report_path)

    command_tokens = []
    command_str = ""

    meta: Dict[str, Any] = {
        "python": python_exe or "",
        "python_resolution": {
            "method": py_method,
            "report_path": report_path,
            "warning": py_warning,
        },
        "git_commit": _git_commit(repo_root),
        "env_vars": {
            k: os.environ.get(k, "")
            for k in [
                "CUDA_VISIBLE_DEVICES",
                "SCIMLOPSBENCH_REPORT",
                "SCIMLOPSBENCH_PYTHON",
            ]
        },
        "timestamp_utc": _utc_timestamp(),
        "decision_reason": "Probe CUDA availability via torch/tensorflow/jax with nvidia-smi fallback.",
    }

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[cuda] stage_dir={stage_dir}\n")
        log_f.write(f"[cuda] report_path={report_path}\n")
        log_f.write(f"[cuda] python_resolution_method={py_method}\n")
        if py_warning:
            log_f.write(f"[cuda] python_resolution_warning={py_warning}\n")

        if not python_exe:
            log_f.write("[cuda] python resolution failed\n")
            failure_category = "missing_report"
            status = "failure"
            exit_code = 1
        elif not _is_executable_file(python_exe):
            log_f.write(f"[cuda] python not executable: {python_exe}\n")
            failure_category = "path_hallucination"
            status = "failure"
            exit_code = 1
        else:
            command_tokens = [python_exe, "-c", CUDA_PROBE_CODE]
            command_str = " ".join(shlex.quote(t) for t in command_tokens)
            log_f.write(f"[cuda] running: {command_str}\n\n")
            log_f.flush()

            try:
                proc = subprocess.run(
                    command_tokens,
                    cwd=str(repo_root),
                    env=os.environ.copy(),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=args.timeout_sec,
                )
                if proc.stdout:
                    log_f.write("[cuda] stdout:\n")
                    log_f.write(proc.stdout)
                    if not proc.stdout.endswith("\n"):
                        log_f.write("\n")
                if proc.stderr:
                    log_f.write("[cuda] stderr:\n")
                    log_f.write(proc.stderr)
                    if not proc.stderr.endswith("\n"):
                        log_f.write("\n")

                log_f.write(f"\n[cuda] probe_exit_code={proc.returncode}\n")
                log_f.flush()

                try:
                    observed = json.loads((proc.stdout or "").strip() or "{}")
                    if isinstance(observed, dict):
                        framework = str(observed.get("framework") or "unknown")
                except Exception as e:
                    observed = {"parse_error": repr(e), "raw_stdout": (proc.stdout or "")[-4000:]}
                    framework = "unknown"

                if proc.returncode == 0:
                    status = "success"
                    exit_code = 0
                    skip_reason = "unknown"
                    failure_category = "unknown"
                elif proc.returncode == 1:
                    # CUDA unavailable is considered a stage failure (insufficient hardware).
                    status = "failure"
                    exit_code = 1
                    skip_reason = "insufficient_hardware"
                    failure_category = "runtime"
                else:
                    status = "failure"
                    exit_code = 1
                    failure_category = "runtime"
            except subprocess.TimeoutExpired:
                log_f.write(f"\n[cuda] timeout after {args.timeout_sec}s\n")
                status = "failure"
                exit_code = 1
                failure_category = "timeout"
            except Exception as e:
                log_f.write(f"\n[cuda] probe execution error: {e}\n")
                status = "failure"
                exit_code = 1
                failure_category = "runtime"

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": int(exit_code),
        "stage": "cuda",
        "task": "check",
        "command": command_str,
        "timeout_sec": int(args.timeout_sec),
        "framework": framework if framework in {"pytorch", "tensorflow", "jax"} else "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "observed": observed,
        "meta": meta,
        "failure_category": failure_category,
        "error_excerpt": _tail_lines(log_path) if status == "failure" else "",
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())

