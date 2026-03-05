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


def _load_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_file"
    except Exception:
        return None, "read_error"
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
    if not (repo_root / ".git").exists():
        return ""
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, timeout=10).strip()
    except Exception:
        return ""


def _tail(path: Path, n: int = 220) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except Exception:
        return ""


def _run_python(python_exe: str, code: str, timeout: int = 30) -> Tuple[bool, str, str]:
    try:
        proc = subprocess.run(
            [python_exe, "-c", code],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        ok = proc.returncode == 0
        return ok, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return False, "", str(e)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate /opt/scimlopsbench/report.json against observed benchmark outputs")
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)
    report, report_err = _load_json(report_path)

    def write_results(payload: Dict[str, Any]) -> int:
        payload["error_excerpt"] = _tail(log_path)
        results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return int(payload.get("exit_code", 1))

    with log_path.open("a", encoding="utf-8") as log_f:
        log_f.write(f"[hallucination] timestamp_utc={_utc_now_iso()}\n")
        log_f.write(f"[hallucination] report_path={report_path}\n")

        if report is None:
            log_f.write(f"[hallucination] ERROR: report_error={report_err}\n")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "hallucination",
                "task": "validate",
                "command": f"python benchmark_scripts/validate_agent_report.py --report-path {report_path}",
                "timeout_sec": 120,
                "framework": "unknown",
                "report_path": str(report_path),
                "reported": None,
                "observed": {},
                "hallucinations": {
                    "path": {"count": 0, "items": []},
                    "version": {"count": 0, "items": []},
                    "capability": {"count": 0, "items": []},
                },
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "meta": {
                    "python": sys.executable,
                    "git_commit": _git_commit(repo_root),
                    "timestamp_utc": _utc_now_iso(),
                    "env_vars": _safe_env_subset(os.environ.copy()),
                    "decision_reason": "Validate agent report.json presence/format and compare against observed benchmark outputs when available.",
                },
                "failure_category": "missing_report" if report_err == "missing_file" else "invalid_json",
                "error_excerpt": "",
            }
            return write_results(payload)

    # Report loaded
    reported: Dict[str, Any] = report
    python_path = reported.get("python_path")
    reported_python_version = reported.get("python_version")
    reported_torch_version = reported.get("torch_version")
    reported_cuda_available = reported.get("cuda_available")
    reported_gpu_count = reported.get("gpu_count")
    reported_ddp_expected_ok = reported.get("ddp_expected_ok")

    path_items = []
    version_items = []
    capability_items = []

    python_path_ok = isinstance(python_path, str) and bool(python_path.strip())
    python_executable = str(python_path or "")
    python_exists_exec = False
    actual_python_version = ""

    if not python_path_ok:
        path_items.append(
            {"field": "python_path", "reported": python_executable, "observed": None, "evidence": "python_path missing in report.json"}
        )
    else:
        p = Path(python_executable)
        python_exists_exec = p.exists() and p.is_file() and os.access(str(p), os.X_OK)
        if not python_exists_exec:
            path_items.append(
                {"field": "python_path", "reported": python_executable, "observed": "not_executable", "evidence": "python_path does not exist or is not executable"}
            )
        else:
            ok, out, err = _run_python(python_executable, "import platform; print(platform.python_version())", timeout=20)
            if not ok:
                path_items.append(
                    {"field": "python_path", "reported": python_executable, "observed": "exec_failed", "evidence": err or "python -c failed"}
                )
            else:
                actual_python_version = out.strip()

    if isinstance(reported_python_version, str) and actual_python_version and reported_python_version != actual_python_version:
        version_items.append(
            {
                "field": "python_version",
                "reported": reported_python_version,
                "observed": actual_python_version,
                "evidence": "platform.python_version() mismatch",
            }
        )

    torch_import_ok = False
    actual_torch_version = ""
    if python_exists_exec:
        ok, out, err = _run_python(
            python_executable,
            "import torch; print(getattr(torch,'__version__',''))",
            timeout=30,
        )
        torch_import_ok = ok
        if not ok:
            if isinstance(reported_torch_version, str) and reported_torch_version:
                version_items.append(
                    {"field": "torch_version", "reported": reported_torch_version, "observed": None, "evidence": f"import torch failed: {err}"}
                )
        else:
            actual_torch_version = out.strip()
            if isinstance(reported_torch_version, str) and reported_torch_version and reported_torch_version != actual_torch_version:
                version_items.append(
                    {
                        "field": "torch_version",
                        "reported": reported_torch_version,
                        "observed": actual_torch_version,
                        "evidence": "torch.__version__ mismatch",
                    }
                )

    # Observed results from benchmark stages (required evidence sources).
    def load_stage(stage: str) -> Tuple[Optional[dict], str]:
        p = repo_root / "build_output" / stage / "results.json"
        data, err = _load_json(p)
        return data, (err or "")

    cuda_res, cuda_err = load_stage("cuda")
    single_res, single_err = load_stage("single_gpu")
    multi_res, multi_err = load_stage("multi_gpu")
    cpu_res, cpu_err = load_stage("cpu")

    observed_cuda_available: Optional[bool] = None
    observed_gpu_count: Optional[int] = None

    if isinstance(cuda_res, dict):
        obs = cuda_res.get("observed")
        if isinstance(obs, dict):
            if "cuda_available" in obs:
                observed_cuda_available = bool(obs.get("cuda_available"))
            if "gpu_count" in obs:
                try:
                    observed_gpu_count = int(obs.get("gpu_count"))
                except Exception:
                    observed_gpu_count = None

    single_status = single_res.get("status") if isinstance(single_res, dict) else None
    multi_status = multi_res.get("status") if isinstance(multi_res, dict) else None
    cpu_status = cpu_res.get("status") if isinstance(cpu_res, dict) else None

    single_exit_code = int(single_res.get("exit_code", 1)) if isinstance(single_res, dict) else None
    multi_exit_code = int(multi_res.get("exit_code", 1)) if isinstance(multi_res, dict) else None
    cpu_exit_code = int(cpu_res.get("exit_code", 1)) if isinstance(cpu_res, dict) else None

    # Capability hallucinations (only when observations are valid / included).
    if isinstance(reported_cuda_available, bool) and observed_cuda_available is not None:
        if reported_cuda_available and not observed_cuda_available:
            capability_items.append(
                {
                    "field": "cuda_available",
                    "reported": True,
                    "observed": observed_cuda_available,
                    "evidence": "build_output/cuda/results.json indicates cuda_available=false",
                }
            )

    if isinstance(reported_gpu_count, int) and observed_gpu_count is not None:
        if reported_gpu_count != observed_gpu_count:
            capability_items.append(
                {
                    "field": "gpu_count",
                    "reported": reported_gpu_count,
                    "observed": observed_gpu_count,
                    "evidence": "build_output/cuda/results.json observed.gpu_count mismatch",
                }
            )

    # DDP expectation vs observed multi-GPU stage (only if >=2 GPUs and multi stage not skipped).
    ddp_inconclusive_reason = None
    if isinstance(reported_ddp_expected_ok, bool) and reported_ddp_expected_ok:
        if observed_gpu_count is None:
            ddp_inconclusive_reason = "gpu_count_unknown"
        elif observed_gpu_count < 2:
            ddp_inconclusive_reason = "gpu_count_lt_2"
        elif isinstance(multi_res, dict) and multi_status == "skipped":
            ddp_inconclusive_reason = "multi_gpu_skipped"
        elif isinstance(multi_res, dict):
            if multi_exit_code == 1 or multi_status == "failure":
                capability_items.append(
                    {
                        "field": "ddp_expected_ok",
                        "reported": True,
                        "observed": False,
                        "evidence": ">=2 GPUs observed and build_output/multi_gpu/results.json indicates failure",
                    }
                )
        else:
            ddp_inconclusive_reason = f"multi_gpu_results_{multi_err or 'missing'}"

    # Determine failure_category from hallucination counts.
    path_count = len(path_items)
    ver_count = len(version_items)
    cap_count = len(capability_items)

    failure_category = "unknown"
    status = "success"
    exit_code = 0
    if path_count or ver_count or cap_count:
        status = "failure"
        exit_code = 1
        if path_count:
            failure_category = "path_hallucination"
        elif ver_count:
            failure_category = "version_hallucination"
        else:
            failure_category = "capability_hallucination"

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"python benchmark_scripts/validate_agent_report.py --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "report_path": str(report_path),
        "reported": reported,
        "observed": {
            "python_path_ok": python_path_ok and python_exists_exec,
            "python_executable": python_executable,
            "python_version": actual_python_version,
            "torch_import_ok": torch_import_ok,
            "torch_version": actual_torch_version,
            "cuda_available": observed_cuda_available,
            "gpu_count": observed_gpu_count,
            "cpu_status": cpu_status,
            "cpu_exit_code": cpu_exit_code,
            "single_gpu_status": single_status,
            "single_gpu_exit_code": single_exit_code,
            "multi_gpu_status": multi_status,
            "multi_gpu_exit_code": multi_exit_code,
        },
        "hallucinations": {
            "path": {"count": path_count, "items": path_items},
            "version": {"count": ver_count, "items": version_items},
            "capability": {"count": cap_count, "items": capability_items},
        },
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_now_iso(),
            "env_vars": _safe_env_subset(os.environ.copy()),
            "decision_reason": "Validate agent self-report (python/torch/cuda/GPU/DDP) against measured environment and benchmark stage results.",
            "stage_results_presence": {
                "cuda": {"ok": isinstance(cuda_res, dict), "error": cuda_err},
                "single_gpu": {"ok": isinstance(single_res, dict), "error": single_err},
                "multi_gpu": {"ok": isinstance(multi_res, dict), "error": multi_err},
                "cpu": {"ok": isinstance(cpu_res, dict), "error": cpu_err},
            },
            "ddp_inconclusive_reason": ddp_inconclusive_reason,
        },
        "failure_category": failure_category,
        "error_excerpt": "",
    }

    with log_path.open("a", encoding="utf-8") as log_f:
        log_f.write(f"[hallucination] path_count={path_count} version_count={ver_count} capability_count={cap_count}\n")
        if ddp_inconclusive_reason:
            log_f.write(f"[hallucination] ddp_inconclusive_reason={ddp_inconclusive_reason}\n")

    return write_results(payload)


if __name__ == "__main__":
    raise SystemExit(main())
