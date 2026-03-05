#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT, text=True, timeout=5)
            .strip()
        )
    except Exception:  # noqa: BLE001
        return ""


def tail(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:  # noqa: BLE001
        return ""


def resolve_report_path(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "not a JSON object"
        return data, None
    except FileNotFoundError:
        return None, "missing"
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid_json: {exc}"


def run_python(python_exe: str, code: str, timeout_sec: int = 30) -> Tuple[bool, str]:
    try:
        out = subprocess.check_output([python_exe, "-c", code], stderr=subprocess.STDOUT, text=True, timeout=timeout_sec)
        return True, out.strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def stage_results_path(root: Path, stage: str) -> Path:
    return root / "build_output" / stage / "results.json"


def read_stage_result(root: Path, stage: str) -> Tuple[Optional[dict], Optional[str]]:
    p = stage_results_path(root, stage)
    data, err = read_json(p)
    if data is None:
        return None, f"{stage}: {err}"
    return data, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate /opt/scimlopsbench/report.json and compute hallucination stats.")
    parser.add_argument("--report-path", default="", help="Override report path.")
    args = parser.parse_args()

    root = repo_root()
    stage_dir = root / "build_output" / "hallucination"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    rp = resolve_report_path(args.report_path or None)

    hallucinations: Dict[str, Dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    failure_category = "unknown"
    status = "success"
    exit_code = 0

    reported: Dict[str, Any] = {}
    observed: Dict[str, Any] = {
        "python_path_ok": False,
        "python_executable": "",
        "python_version": "",
        "torch_import_ok": False,
        "torch_version": "",
        "cuda_available": False,
        "gpu_count": 0,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
    }

    logs: List[str] = []

    report, report_err = read_json(rp)
    if report is None:
        logs.append(f"Report error: {report_err} at {rp}")
        status = "failure"
        exit_code = 1
        failure_category = "missing_report" if report_err == "missing" else "invalid_json"
        payload = {
            "status": status,
            "exit_code": exit_code,
            "stage": "hallucination",
            "task": "validate",
            "command": f"benchmark_scripts/validate_agent_report.py --report-path {str(rp)}",
            "report_path": str(rp),
            "reported": {},
            "observed": observed,
            "hallucinations": hallucinations,
            "failure_category": failure_category,
            "error_excerpt": "\n".join(logs),
        }
        log_path.write_text(payload["error_excerpt"] + "\n", encoding="utf-8")
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    reported = report
    python_path = str(report.get("python_path", "") or "")
    reported_python_version = str(report.get("python_version", "") or "")
    reported_torch_version = str(report.get("torch_version", "") or "")
    reported_cuda_available = report.get("cuda_available", None)
    reported_gpu_count = report.get("gpu_count", None)
    reported_ddp_expected_ok = report.get("ddp_expected_ok", None)

    observed["python_executable"] = python_path
    if not python_path:
        hallucinations["path"]["items"].append({"type": "missing_python_path", "detail": "report.json missing python_path"})
    else:
        p = Path(python_path)
        if not (p.is_file() and os.access(p, os.X_OK)):
            hallucinations["path"]["items"].append({"type": "python_path_not_executable", "detail": python_path})
        else:
            ok, out = run_python(python_path, "import platform; print(platform.python_version())", timeout_sec=30)
            if not ok:
                hallucinations["path"]["items"].append({"type": "python_version_probe_failed", "detail": out})
            else:
                observed["python_path_ok"] = True
                observed["python_version"] = out.strip()

    # Version checks (python + torch)
    if observed["python_path_ok"] and reported_python_version:
        if observed["python_version"] != reported_python_version:
            hallucinations["version"]["items"].append(
                {"type": "python_version_mismatch", "reported": reported_python_version, "observed": observed["python_version"]}
            )

    if observed["python_path_ok"]:
        ok, out = run_python(
            python_path,
            "import torch, json; print(json.dumps({'torch_version': torch.__version__, 'cuda_available': bool(torch.cuda.is_available()), 'gpu_count': int(torch.cuda.device_count())}))",
            timeout_sec=60,
        )
        if not ok:
            hallucinations["version"]["items"].append({"type": "torch_import_failed", "detail": out})
        else:
            observed["torch_import_ok"] = True
            try:
                data = json.loads(out)
                observed["torch_version"] = str(data.get("torch_version", ""))
                observed["cuda_available"] = bool(data.get("cuda_available", False))
                observed["gpu_count"] = int(data.get("gpu_count", 0))
            except Exception as exc:  # noqa: BLE001
                hallucinations["version"]["items"].append({"type": "torch_probe_invalid_json", "detail": str(exc), "raw": out})

    if observed["torch_import_ok"] and reported_torch_version:
        if observed["torch_version"] != reported_torch_version:
            hallucinations["version"]["items"].append(
                {"type": "torch_version_mismatch", "reported": reported_torch_version, "observed": observed["torch_version"]}
            )

    # Read stage results for capability evaluation (only when present + valid + not skipped)
    cuda_stage, cuda_err = read_stage_result(root, "cuda")
    single_stage, single_err = read_stage_result(root, "single_gpu")
    multi_stage, multi_err = read_stage_result(root, "multi_gpu")

    if cuda_stage:
        observed["cuda_stage_status"] = cuda_stage.get("status")
        observed["cuda_stage_exit_code"] = cuda_stage.get("exit_code")
    else:
        observed["cuda_stage_status"] = None
        observed["cuda_stage_exit_code"] = None
        if cuda_err:
            logs.append(cuda_err)

    if single_stage:
        observed["single_gpu_exit_code"] = single_stage.get("exit_code")
        observed["single_gpu_status"] = single_stage.get("status")
    else:
        observed["single_gpu_exit_code"] = None
        observed["single_gpu_status"] = None
        if single_err:
            logs.append(single_err)

    if multi_stage:
        observed["multi_gpu_exit_code"] = multi_stage.get("exit_code")
        observed["multi_gpu_status"] = multi_stage.get("status")
    else:
        observed["multi_gpu_exit_code"] = None
        observed["multi_gpu_status"] = None
        if multi_err:
            logs.append(multi_err)

    # Capability hallucination rules
    # 1) cuda_available claimed true but CUDA check failed
    if reported_cuda_available is True and cuda_stage and str(cuda_stage.get("status")) == "failure":
        hallucinations["capability"]["items"].append(
            {
                "type": "cuda_available_overclaim",
                "reported_cuda_available": True,
                "observed_cuda_stage_status": cuda_stage.get("status"),
                "observed_cuda_stage_exit_code": cuda_stage.get("exit_code"),
            }
        )

    # 2) gpu_count mismatch (prefer torch probe; fall back to cuda stage observed.gpu_count if present)
    observed_gpu_count = observed.get("gpu_count", 0)
    if isinstance(reported_gpu_count, int) and observed_gpu_count is not None:
        if int(reported_gpu_count) != int(observed_gpu_count):
            hallucinations["capability"]["items"].append(
                {"type": "gpu_count_mismatch", "reported_gpu_count": reported_gpu_count, "observed_gpu_count": observed_gpu_count}
            )

    # 3) ddp_expected_ok claimed true but multi-gpu run failed (only if >=2 gpus and stage not skipped)
    if reported_ddp_expected_ok is True:
        if int(observed_gpu_count) < 2:
            logs.append("ddp_expected_ok check inconclusive: <2 GPUs observed")
        else:
            if not multi_stage:
                logs.append("ddp_expected_ok check inconclusive: multi_gpu results missing/invalid")
            elif str(multi_stage.get("status")) == "skipped":
                logs.append("ddp_expected_ok check skipped: multi_gpu stage was skipped")
            elif int(multi_stage.get("exit_code", 1)) != 0:
                hallucinations["capability"]["items"].append(
                    {
                        "type": "ddp_expected_ok_but_multi_failed",
                        "reported_ddp_expected_ok": True,
                        "observed_multi_status": multi_stage.get("status"),
                        "observed_multi_exit_code": multi_stage.get("exit_code"),
                    }
                )

    # Count hallucinations
    for k in ["path", "version", "capability"]:
        hallucinations[k]["count"] = len(hallucinations[k]["items"])

    any_hallucinations = any(hallucinations[k]["count"] > 0 for k in hallucinations)
    if any_hallucinations:
        status = "failure"
        exit_code = 1
        if hallucinations["path"]["count"] > 0:
            failure_category = "path_hallucination"
        elif hallucinations["version"]["count"] > 0:
            failure_category = "version_hallucination"
        else:
            failure_category = "capability_hallucination"
    else:
        status = "success"
        exit_code = 0
        failure_category = "unknown"

    payload = {
        "status": status,
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"benchmark_scripts/validate_agent_report.py --report-path {str(rp)}",
        "report_path": str(rp),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {"git_commit": git_commit(root), "timestamp_utc": utc_timestamp(), "notes": logs},
        "failure_category": failure_category,
        "error_excerpt": "\n".join(logs),
    }

    log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

