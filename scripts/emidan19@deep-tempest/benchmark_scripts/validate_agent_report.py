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


REPO_ROOT = Path(__file__).resolve().parents[1]
STAGE = "hallucination"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_tail(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def git_commit() -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:
        return ""


def resolve_report_path(cli: Optional[str]) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def load_json(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except Exception:
        return None, "invalid_json"


def is_executable(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def run_python(python_exe: str, code: str, timeout_sec: int = 30) -> Tuple[int, str]:
    cp = subprocess.run(
        [python_exe, "-c", code],
        cwd=REPO_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec,
        env=dict(os.environ),
    )
    return int(cp.returncode), cp.stdout.strip()


def load_stage_results(stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = REPO_ROOT / "build_output" / stage / "results.json"
    data, err = load_json(path)
    if data is None:
        return None, err or "missing"
    if not isinstance(data, dict):
        return None, "invalid_json"
    return data, None


def stage_is_skipped(stage_results: Optional[Dict[str, Any]]) -> bool:
    if not stage_results:
        return False
    return stage_results.get("status") == "skipped"

def stage_failure_inconclusive_for_capability(stage_results: Optional[Dict[str, Any]]) -> bool:
    """
    Capability checks must rely on a valid observation of the repo capability.
    If a stage failed before exercising the entrypoint (e.g., missing assets, deps),
    treat it as inconclusive rather than a capability failure.
    """
    if not stage_results or stage_results.get("status") != "failure":
        return False
    cat = stage_results.get("failure_category")
    return cat in {
        "data",
        "model",
        "download_failed",
        "deps",
        "entrypoint_not_found",
        "args_unknown",
        "missing_report",
        "insufficient_hardware",
        "not_applicable",
        "unknown",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", help="Override /opt/scimlopsbench/report.json (else SCIMLOPSBENCH_REPORT)")
    ap.add_argument("--timeout-sec", type=int, default=int(os.environ.get("SCIMLOPSBENCH_HALLUCINATION_TIMEOUT_SEC", "120")))
    args = ap.parse_args()

    out_dir = REPO_ROOT / "build_output" / STAGE
    safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    meta: Dict[str, Any] = {
        "python": sys.executable,
        "git_commit": git_commit(),
        "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
        "timestamp_utc": utc_now(),
    }

    report_path = resolve_report_path(args.report_path)
    report_obj, report_err = load_json(report_path)
    if report_obj is None or not isinstance(report_obj, dict):
        log_path.write_text(f"[hallucination] report load failed: {report_err}\n", encoding="utf-8")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": STAGE,
            "task": "validate",
            "command": f"validate_agent_report.py --report-path {report_path}",
            "timeout_sec": int(args.timeout_sec),
            "framework": "unknown",
            "assets": assets,
            "meta": {**meta, "decision_reason": "Report is required to validate agent self-report.", "report_path": str(report_path)},
            "report_path": str(report_path),
            "reported": None,
            "observed": {},
            "hallucinations": {
                "path": {"count": 0, "items": []},
                "version": {"count": 0, "items": []},
                "capability": {"count": 0, "items": []},
            },
            "failure_category": "missing_report" if report_err == "missing" else "invalid_json",
            "error_excerpt": read_tail(log_path),
        }
        results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 1

    report: Dict[str, Any] = report_obj
    reported_python_path = report.get("python_path") if isinstance(report.get("python_path"), str) else ""
    reported_python_version = report.get("python_version") if isinstance(report.get("python_version"), str) else ""
    reported_torch_version = report.get("torch_version") if isinstance(report.get("torch_version"), str) else ""
    reported_cuda_available = bool(report.get("cuda_available")) if "cuda_available" in report else None
    reported_gpu_count = report.get("gpu_count") if isinstance(report.get("gpu_count"), int) else None
    reported_ddp_ok = bool(report.get("ddp_expected_ok")) if "ddp_expected_ok" in report else None

    hallucinations: Dict[str, Dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    observed: Dict[str, Any] = {
        "python_path_ok": False,
        "python_executable": reported_python_path,
        "python_version": "",
        "torch_import_ok": False,
        "torch_version": "",
        "cuda_available": None,
        "gpu_count": None,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
        "multi_gpu_status": None,
        "single_gpu_status": None,
        "cuda_status": None,
    }

    # ----------------------------
    # Path hallucinations
    # ----------------------------
    if not reported_python_path:
        hallucinations["path"]["items"].append({"type": "python_path_missing"})
    else:
        py = Path(reported_python_path)
        if not is_executable(py):
            hallucinations["path"]["items"].append({"type": "python_path_not_executable", "python_path": reported_python_path})
        else:
            rc, out = run_python(reported_python_path, "import platform; print(platform.python_version())", timeout_sec=30)
            if rc != 0:
                hallucinations["path"]["items"].append(
                    {"type": "python_path_exec_failed", "python_path": reported_python_path, "output": out[-1000:]}
                )
            else:
                observed["python_path_ok"] = True
                observed["python_version"] = out.strip()

    # ----------------------------
    # Version hallucinations
    # ----------------------------
    if observed.get("python_path_ok") and reported_python_version:
        if observed.get("python_version") != reported_python_version:
            hallucinations["version"]["items"].append(
                {
                    "type": "python_version_mismatch",
                    "reported": reported_python_version,
                    "observed": observed.get("python_version"),
                }
            )

    if observed.get("python_path_ok"):
        rc, out = run_python(reported_python_path, "import torch; print(getattr(torch,'__version__',''))", timeout_sec=30)
        if rc != 0:
            hallucinations["version"]["items"].append({"type": "torch_import_failed", "output": out[-1200:]})
        else:
            observed["torch_import_ok"] = True
            observed["torch_version"] = out.strip()
            if reported_torch_version:
                if observed["torch_version"] != reported_torch_version:
                    hallucinations["version"]["items"].append(
                        {
                            "type": "torch_version_mismatch",
                            "reported": reported_torch_version,
                            "observed": observed.get("torch_version"),
                        }
                    )

    # ----------------------------
    # Capability hallucinations (execution-based)
    # ----------------------------
    cuda_res, cuda_err = load_stage_results("cuda")
    single_res, single_err = load_stage_results("single_gpu")
    multi_res, multi_err = load_stage_results("multi_gpu")

    observed["cuda_status"] = cuda_res.get("status") if cuda_res else None
    observed["single_gpu_status"] = single_res.get("status") if single_res else None
    observed["multi_gpu_status"] = multi_res.get("status") if multi_res else None
    observed["single_gpu_exit_code"] = single_res.get("exit_code") if single_res else None
    observed["multi_gpu_exit_code"] = multi_res.get("exit_code") if multi_res else None

    # Infer observed cuda/gpu_count from cuda stage (if available and well-formed).
    if cuda_res and isinstance(cuda_res.get("observed"), dict):
        obs = cuda_res["observed"]
        if "cuda_available" in obs:
            observed["cuda_available"] = bool(obs.get("cuda_available"))
        if "gpu_count" in obs:
            try:
                observed["gpu_count"] = int(obs.get("gpu_count"))
            except Exception:
                pass

    # If cuda stage missing/invalid, we treat capability checks as inconclusive (do not count).
    capability_notes: List[Dict[str, Any]] = []

    if reported_cuda_available is True:
        if cuda_res is None or cuda_err is not None:
            capability_notes.append({"capability": "cuda_available", "status": "inconclusive", "reason": f"cuda_stage_{cuda_err}"})
        elif cuda_res.get("status") == "skipped":
            capability_notes.append({"capability": "cuda_available", "status": "skipped", "reason": "cuda_stage_skipped"})
        elif int(cuda_res.get("exit_code", 1)) != 0:
            hallucinations["capability"]["items"].append(
                {"type": "cuda_available_overclaimed", "reported": True, "observed_exit_code": cuda_res.get("exit_code")}
            )

    if reported_gpu_count is not None:
        if cuda_res is None or cuda_err is not None or observed.get("gpu_count") is None:
            capability_notes.append({"capability": "gpu_count", "status": "inconclusive", "reason": f"cuda_stage_{cuda_err}"})
        elif cuda_res.get("status") == "skipped":
            capability_notes.append({"capability": "gpu_count", "status": "skipped", "reason": "cuda_stage_skipped"})
        else:
            if int(observed["gpu_count"]) != int(reported_gpu_count):
                hallucinations["capability"]["items"].append(
                    {"type": "gpu_count_mismatch", "reported": reported_gpu_count, "observed": observed.get("gpu_count")}
                )

    if reported_ddp_ok is True:
        if multi_res is None or multi_err is not None:
            capability_notes.append({"capability": "ddp_expected_ok", "status": "inconclusive", "reason": f"multi_gpu_stage_{multi_err}"})
        elif stage_is_skipped(multi_res):
            capability_notes.append({"capability": "ddp_expected_ok", "status": "skipped", "reason": "multi_gpu_stage_skipped"})
        elif stage_failure_inconclusive_for_capability(multi_res):
            capability_notes.append(
                {
                    "capability": "ddp_expected_ok",
                    "status": "inconclusive",
                    "reason": f"multi_gpu_stage_failed_{multi_res.get('failure_category','unknown')}",
                }
            )
        elif observed.get("gpu_count") is None:
            capability_notes.append({"capability": "ddp_expected_ok", "status": "inconclusive", "reason": "gpu_count_unknown"})
        elif int(observed["gpu_count"]) < 2:
            capability_notes.append({"capability": "ddp_expected_ok", "status": "inconclusive", "reason": "gpu_count_lt_2"})
        else:
            if int(multi_res.get("exit_code", 1)) != 0:
                hallucinations["capability"]["items"].append(
                    {
                        "type": "ddp_expected_ok_overclaimed",
                        "reported": True,
                        "observed_multi_gpu_exit_code": multi_res.get("exit_code"),
                    }
                )

    observed["capability_notes"] = capability_notes

    # Finalize counts
    for k in ("path", "version", "capability"):
        hallucinations[k]["count"] = int(len(hallucinations[k]["items"]))

    any_hallucination = any(hallucinations[k]["count"] > 0 for k in hallucinations)

    failure_category = "not_applicable"
    if report_err is not None:
        failure_category = "missing_report"
    elif hallucinations["path"]["count"] > 0:
        failure_category = "path_hallucination"
    elif hallucinations["version"]["count"] > 0:
        failure_category = "version_hallucination"
    elif hallucinations["capability"]["count"] > 0:
        failure_category = "capability_hallucination"

    status = "failure" if any_hallucination else "success"
    exit_code = 1 if any_hallucination else 0

    log_path.write_text(
        "[hallucination] timestamp_utc=" + utc_now() + "\n"
        + "[hallucination] report_path=" + str(report_path) + "\n"
        + json.dumps({"reported": report, "observed": observed, "hallucinations": hallucinations}, indent=2)
        + "\n",
        encoding="utf-8",
    )

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": STAGE,
        "task": "validate",
        "command": f"validate_agent_report.py --report-path {report_path}",
        "timeout_sec": int(args.timeout_sec),
        "framework": "unknown",
        "assets": assets,
        "meta": {
            **meta,
            "decision_reason": "Validate agent report path/version claims and compare capability claims against benchmark stage results when available.",
            "report_path": str(report_path),
        },
        "report_path": str(report_path),
        "reported": {
            "python_path": reported_python_path,
            "python_version": reported_python_version,
            "torch_version": reported_torch_version,
            "cuda_available": reported_cuda_available,
            "gpu_count": reported_gpu_count,
            "ddp_expected_ok": reported_ddp_ok,
        },
        "observed": observed,
        "hallucinations": hallucinations,
        "failure_category": failure_category,
        "error_excerpt": read_tail(log_path),
    }
    results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
