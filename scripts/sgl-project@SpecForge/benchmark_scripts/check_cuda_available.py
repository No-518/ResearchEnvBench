#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def _load_report(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception:
        return None, "missing_report"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(data, dict):
        return None, "invalid_json"
    return data, None


def _safe_env_subset(env: Dict[str, str]) -> Dict[str, str]:
    keep_prefixes = ("SCIMLOPSBENCH_", "CUDA_", "HF_", "TRANSFORMERS_", "TORCH", "PYTHON", "WANDB_")
    keep_keys = {"PATH", "HOME", "USER", "SHELL", "PWD", "VIRTUAL_ENV", "CONDA_DEFAULT_ENV", "CONDA_PREFIX"}
    out: Dict[str, str] = {}
    for k, v in env.items():
        if k in keep_keys or any(k.startswith(p) for p in keep_prefixes):
            out[k] = v
    return out


def _git_commit(repo_root: Path) -> str:
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return ""
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, timeout=10).strip()
    except Exception:
        return ""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CUDA availability check (torch/tf/jax)")
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--python", dest="python_bin", default=None, help="Override python executable used for checks")
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "cuda"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)
    python_exe: Optional[str] = None
    python_resolution = "unknown"

    if args.python_bin:
        python_exe = args.python_bin
        python_resolution = "cli"
    elif os.environ.get("SCIMLOPSBENCH_PYTHON"):
        python_exe = os.environ["SCIMLOPSBENCH_PYTHON"]
        python_resolution = "env:SCIMLOPSBENCH_PYTHON"
    else:
        report, err = _load_report(report_path)
        if report is not None and isinstance(report.get("python_path"), str) and report["python_path"].strip():
            python_exe = report["python_path"]
            python_resolution = "report:python_path"
        else:
            python_exe = None
            python_resolution = f"report_error:{err or 'missing_python_path'}"

    cmd = [
        python_exe or "",
        "-c",
        (
            "import json\n"
            "out={'framework':'unknown','cuda_available':False,'gpu_count':0,'details':{}}\n"
            "try:\n"
            "  import torch\n"
            "  out['framework']='pytorch'\n"
            "  out['details']['torch_version']=getattr(torch,'__version__',None)\n"
            "  out['cuda_available']=bool(torch.cuda.is_available())\n"
            "  out['gpu_count']=int(torch.cuda.device_count())\n"
            "except Exception as e:\n"
            "  out['details']['torch_error']=str(e)\n"
            "if out['framework']=='unknown':\n"
            "  try:\n"
            "    import tensorflow as tf\n"
            "    out['framework']='tensorflow'\n"
            "    out['details']['tf_version']=getattr(tf,'__version__',None)\n"
            "    gpus=tf.config.list_physical_devices('GPU')\n"
            "    out['gpu_count']=len(gpus)\n"
            "    out['cuda_available']=out['gpu_count']>0\n"
            "  except Exception as e:\n"
            "    out['details']['tf_error']=str(e)\n"
            "if out['framework']=='unknown':\n"
            "  try:\n"
            "    import jax\n"
            "    out['framework']='jax'\n"
            "    out['details']['jax_version']=getattr(jax,'__version__',None)\n"
            "    gpus=[d for d in jax.devices() if d.platform=='gpu']\n"
            "    out['gpu_count']=len(gpus)\n"
            "    out['cuda_available']=out['gpu_count']>0\n"
            "  except Exception as e:\n"
            "    out['details']['jax_error']=str(e)\n"
            "print(json.dumps(out))\n"
        ),
    ]

    base_results: Dict[str, Any] = {
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
            "python_resolution": python_resolution,
            "report_path": str(report_path),
            "git_commit": _git_commit(repo_root),
            "env_vars": _safe_env_subset(os.environ.copy()),
            "decision_reason": "Checks CUDA availability using the agent-reported python environment via torch/tensorflow/jax imports.",
            "timestamp_utc": _utc_now_iso(),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    def finalize(
        exit_code: int,
        status: str,
        failure_category: str,
        command: str,
        observed: Optional[Dict[str, Any]] = None,
    ) -> int:
        base_results["exit_code"] = exit_code
        base_results["status"] = status
        base_results["failure_category"] = failure_category
        base_results["command"] = command
        if observed is not None:
            base_results["observed"] = observed
            base_results["framework"] = observed.get("framework", "unknown")
        try:
            base_results["error_excerpt"] = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:]
            base_results["error_excerpt"] = "\n".join(base_results["error_excerpt"])
        except Exception:
            base_results["error_excerpt"] = ""
        results_path.write_text(json.dumps(base_results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return exit_code

    with log_path.open("a", encoding="utf-8") as log_f:
        log_f.write(f"[cuda] timestamp_utc={_utc_now_iso()}\n")
        log_f.write(f"[cuda] report_path={report_path}\n")
        log_f.write(f"[cuda] python_exe={python_exe or ''} ({python_resolution})\n")
        if not python_exe:
            log_f.write("[cuda] ERROR: missing_report (cannot resolve python_path)\n")
            return finalize(1, "failure", "missing_report", "")

        command_str = f"{python_exe} -c <cuda_check_snippet>"
        log_f.write(f"[cuda] command={command_str}\n")
        log_f.flush()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            log_f.write("[cuda] ERROR: timeout\n")
            return finalize(1, "failure", "timeout", command_str)

        log_f.write(proc.stdout)
        if proc.stderr:
            log_f.write("\n[cuda][stderr]\n")
            log_f.write(proc.stderr)
        log_f.flush()

    try:
        observed = json.loads(proc.stdout.strip() or "{}")
        if not isinstance(observed, dict):
            observed = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "details": {"parse_error": "non-dict"}}
    except Exception as e:
        observed = {"framework": "unknown", "cuda_available": False, "gpu_count": 0, "details": {"parse_error": str(e)}}

    cuda_available = bool(observed.get("cuda_available"))
    if cuda_available:
        return finalize(0, "success", "unknown", f"{python_exe} -c <cuda_check_snippet>", observed=observed)
    return finalize(1, "failure", "runtime", f"{python_exe} -c <cuda_check_snippet>", observed=observed)


if __name__ == "__main__":
    raise SystemExit(main())
