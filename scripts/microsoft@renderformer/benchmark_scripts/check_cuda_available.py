#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git_commit(repo_root: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:
        return ""


def _tail_lines(text: str, max_lines: int = 220) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _safe_json_load(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except Exception as e:
        return None, f"invalid json: {path}: {e}"


def _resolve_report_path(cli: Optional[str]) -> str:
    if cli:
        return cli
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return os.environ["SCIMLOPSBENCH_REPORT"]
    return DEFAULT_REPORT_PATH


def _resolve_python_from_report(report_path: str) -> Tuple[Optional[str], Optional[str]]:
    data, err = _safe_json_load(Path(report_path))
    if data is None:
        return None, err
    python_path = data.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        return None, "python_path missing in report"
    return python_path, None


def _run_probe(python_path: str, timeout_sec: int = 30) -> Tuple[int, str, str]:
    code = r"""
import json, sys
meta = {}
try:
  import torch
  framework="pytorch"
  cuda_available=bool(torch.cuda.is_available())
  gpu_count=int(torch.cuda.device_count())
  meta["torch_version"]=getattr(torch,"__version__","")
  meta["torch_cuda_version"]=getattr(getattr(torch,"version",None),"cuda","") if hasattr(torch,"version") else ""
except Exception as e:
  try:
    import tensorflow as tf
    framework="tensorflow"
    gpus=tf.config.list_physical_devices("GPU")
    cuda_available=len(gpus)>0
    gpu_count=len(gpus)
    meta["tensorflow_version"]=getattr(tf,"__version__","")
  except Exception as e2:
    try:
      import jax
      framework="jax"
      devices=jax.devices()
      gpu_devices=[d for d in devices if getattr(d,"platform","")=="gpu"]
      cuda_available=len(gpu_devices)>0
      gpu_count=len(gpu_devices)
      meta["jax_version"]=getattr(jax,"__version__","")
    except Exception as e3:
      framework="unknown"
      cuda_available=False
      gpu_count=0
      meta["torch_import_error"]=str(e)
      meta["tensorflow_import_error"]=str(e2)
      meta["jax_import_error"]=str(e3)
print(json.dumps({"framework": framework, "cuda_available": cuda_available, "gpu_count": gpu_count, "meta": meta}))
"""
    try:
        res = subprocess.run(
            [python_path, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def main() -> int:
    report_path_cli = None
    if "--report-path" in sys.argv:
        idx = sys.argv.index("--report-path")
        if idx + 1 < len(sys.argv):
            report_path_cli = sys.argv[idx + 1]

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    logs: list[str] = []
    logs.append(f"[cuda] timestamp_utc={_utc_timestamp()}")
    logs.append(f"[cuda] sys.executable={sys.executable}")
    logs.append(f"[cuda] CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES','')}")

    report_path = _resolve_report_path(report_path_cli)
    logs.append(f"[cuda] report_path={report_path}")
    python_path, py_err = _resolve_python_from_report(report_path)

    framework = "unknown"
    cuda_available = False
    gpu_count = 0
    fw_meta: Dict[str, Any] = {}

    failure_category = "runtime"
    if python_path is None:
        logs.append(f"[cuda] python_resolution_error={py_err}")
        failure_category = "missing_report"
    else:
        logs.append(f"[cuda] reported_python_path={python_path}")
        rc, out, err = _run_probe(python_path)
        if rc != 0 or not out:
            logs.append(f"[cuda] probe_failed rc={rc}")
            if err:
                logs.append(f"[cuda] probe_stderr={err}")
            failure_category = "runtime"
        else:
            try:
                d = json.loads(out)
                framework = str(d.get("framework", "unknown"))
                cuda_available = bool(d.get("cuda_available", False))
                gpu_count = int(d.get("gpu_count", 0))
                fw_meta = d.get("meta", {}) if isinstance(d.get("meta"), dict) else {}
            except Exception as e:
                logs.append(f"[cuda] probe_output_parse_error={e}")
                logs.append(f"[cuda] probe_stdout={out[:2000]}")
                failure_category = "invalid_json"

    logs.append(f"[cuda] framework={framework}")
    logs.append(f"[cuda] cuda_available={cuda_available}")
    logs.append(f"[cuda] gpu_count={gpu_count}")
    for k, v in fw_meta.items():
        logs.append(f"[cuda] {k}={v}")

    status = "success" if cuda_available else "failure"
    exit_code = 0 if cuda_available else 1
    if cuda_available:
        failure_category = "unknown"

    log_text = "\n".join(logs) + "\n"
    log_path.write_text(log_text, encoding="utf-8")

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "cuda",
        "task": "check",
        "command": f"{sys.executable} benchmark_scripts/check_cuda_available.py",
        "timeout_sec": 120,
        "framework": framework,
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo_root),
            "env_vars": {k: os.environ.get(k, "") for k in ["CUDA_VISIBLE_DEVICES"] if os.environ.get(k)},
            "decision_reason": "Prefer torch if available; else tensorflow; else jax.",
            "timestamp_utc": _utc_timestamp(),
        },
        "observed": {
            "cuda_available": cuda_available,
            "gpu_count": gpu_count,
        },
        "failure_category": failure_category,
        "error_excerpt": _tail_lines(log_text, max_lines=220),
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
