#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bench_utils import REPO_ROOT, ensure_dir, get_git_commit, tail_lines, utc_timestamp, write_json


def resolve_report_path(cli_report_path: str) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path("/opt/scimlopsbench/report.json")


def safe_read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data if isinstance(data, dict) else None), None
    except FileNotFoundError:
        return None, "missing_report"
    except Exception:
        return None, "invalid_json"


def run_python_probe(python_exe: str, code: str, timeout_sec: int = 30) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [python_exe, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as e:
        return 127, "", f"FileNotFoundError: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", "TimeoutExpired"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def load_stage_result(stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = REPO_ROOT / "build_output" / stage / "results.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception:
        return None, "invalid_json"


def stage_outcome(stage_result: Optional[Dict[str, Any]]) -> Tuple[str, int]:
    if not isinstance(stage_result, dict):
        return "inconclusive", 1
    status = stage_result.get("status", "failure")
    exit_code = int(stage_result.get("exit_code", 1) or 1)
    return str(status), exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate agent report and compute hallucination statistics.")
    parser.add_argument("--report-path", default="", help="Override report path.")
    args = parser.parse_args()

    stage = "hallucination"
    out_dir = REPO_ROOT / "build_output" / stage
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report, report_err = safe_read_json(report_path)

    hallucinations: Dict[str, Dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    observed: Dict[str, Any] = {
        "python_path_ok": False,
        "python_executable": "",
        "python_version": "",
        "torch_import_ok": False,
        "torch_version": "",
        "cuda_available": None,
        "gpu_count": None,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
    }

    reported: Dict[str, Any] = report or {}

    log_lines: List[str] = []
    log_lines.append(f"timestamp_utc={utc_timestamp()}")
    log_lines.append(f"report_path={report_path}")

    if not report:
        hallucinations["path"]["items"].append({"type": "report_missing_or_invalid", "detail": report_err or "unknown"})
        hallucinations["path"]["count"] = 1

        log_lines.append(f"ERROR: report not readable ({report_err})")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        write_json(
            results_path,
            {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "hallucination",
                "task": "validate",
                "command": f"python {Path(__file__).name} --report-path {report_path}",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "meta": {
                    "python": "",
                    "git_commit": get_git_commit(REPO_ROOT),
                    "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
                    "decision_reason": "Cannot validate without a readable agent report.",
                },
                "report_path": str(report_path),
                "reported": reported,
                "observed": observed,
                "hallucinations": hallucinations,
                "failure_category": report_err or "missing_report",
                "error_excerpt": tail_lines(log_path),
            },
        )
        return 1

    python_exe = report.get("python_path")
    observed["python_executable"] = python_exe if isinstance(python_exe, str) else ""

    # --- Path hallucinations ---
    if not isinstance(python_exe, str) or not python_exe:
        hallucinations["path"]["items"].append({"type": "python_path_missing", "detail": "report.python_path is missing/empty"})
    else:
        py_path = Path(python_exe)
        if not py_path.exists():
            hallucinations["path"]["items"].append({"type": "python_path_not_found", "detail": python_exe})
        elif not os.access(py_path, os.X_OK):
            hallucinations["path"]["items"].append({"type": "python_path_not_executable", "detail": python_exe})
        else:
            rc, out, err = run_python_probe(python_exe, 'import platform; print(platform.python_version())')
            if rc != 0:
                hallucinations["path"]["items"].append(
                    {"type": "python_invocation_failed", "detail": f"rc={rc} err={err.strip()}"}
                )
            else:
                observed["python_path_ok"] = True
                observed["python_version"] = out.strip().splitlines()[-1] if out.strip() else ""

    hallucinations["path"]["count"] = len(hallucinations["path"]["items"])

    # --- Version hallucinations (python, torch) ---
    reported_py_ver = report.get("python_version")
    if isinstance(reported_py_ver, str) and reported_py_ver and observed.get("python_version"):
        if str(observed["python_version"]) != reported_py_ver:
            hallucinations["version"]["items"].append(
                {"type": "python_version_mismatch", "reported": reported_py_ver, "observed": observed["python_version"]}
            )

    # Torch probe (only if python path OK)
    torch_probe = r"""
import json
out = {"torch_import_ok": False, "torch_version": "", "cuda_available": None, "gpu_count": None}
try:
  import torch
  out["torch_import_ok"] = True
  out["torch_version"] = getattr(torch, "__version__", "")
  out["cuda_available"] = bool(torch.cuda.is_available())
  out["gpu_count"] = int(torch.cuda.device_count())
except Exception as e:
  out["torch_error"] = f"{type(e).__name__}: {e}"
print(json.dumps(out))
"""
    if observed.get("python_path_ok") and isinstance(python_exe, str) and python_exe:
        rc, out, err = run_python_probe(python_exe, torch_probe)
        log_lines.append(f"torch_probe_rc={rc}")
        if err:
            log_lines.append(f"torch_probe_stderr={err.strip()}")
        try:
            td = json.loads(out.strip() or "{}")
        except Exception:
            td = {}
        observed["torch_import_ok"] = bool(td.get("torch_import_ok", False))
        observed["torch_version"] = td.get("torch_version", "") if isinstance(td.get("torch_version", ""), str) else ""
        observed["cuda_available"] = td.get("cuda_available", None)
        observed["gpu_count"] = td.get("gpu_count", None)

        reported_torch_ver = report.get("torch_version")
        if isinstance(reported_torch_ver, str) and reported_torch_ver:
            if not observed["torch_import_ok"]:
                hallucinations["version"]["items"].append(
                    {"type": "torch_import_failed", "reported": reported_torch_ver, "detail": td.get("torch_error", "")}
                )
            elif observed.get("torch_version") and observed["torch_version"] != reported_torch_ver:
                hallucinations["version"]["items"].append(
                    {"type": "torch_version_mismatch", "reported": reported_torch_ver, "observed": observed["torch_version"]}
                )

    hallucinations["version"]["count"] = len(hallucinations["version"]["items"])

    # --- Capability hallucinations (based on stage results) ---
    cuda_res, _ = load_stage_result("cuda")
    single_res, _ = load_stage_result("single_gpu")
    multi_res, _ = load_stage_result("multi_gpu")
    cpu_res, _ = load_stage_result("cpu")

    cuda_status, cuda_exit = stage_outcome(cuda_res)
    single_status, single_exit = stage_outcome(single_res)
    multi_status, multi_exit = stage_outcome(multi_res)
    cpu_status, cpu_exit = stage_outcome(cpu_res)

    observed["single_gpu_exit_code"] = single_exit if single_status != "inconclusive" else None
    observed["multi_gpu_exit_code"] = multi_exit if multi_status != "inconclusive" else None

    # Prefer cuda stage's observed gpu_count/cuda_available if present.
    if isinstance(cuda_res, dict):
        obs = cuda_res.get("observed", {})
        if isinstance(obs, dict):
            if "cuda_available" in obs:
                observed["cuda_available"] = obs.get("cuda_available")
            if "gpu_count" in obs:
                observed["gpu_count"] = obs.get("gpu_count")

    # Derive observed CUDA availability from (a) cuda stage if valid, else (b) torch probe if valid.
    cuda_observed_available: Optional[bool] = None
    if cuda_status in ("success", "failure") and cuda_status != "skipped":
        cuda_observed_available = cuda_exit == 0
    elif isinstance(observed.get("cuda_available"), bool):
        cuda_observed_available = bool(observed["cuda_available"])

    reported_cuda_avail = report.get("cuda_available")
    if reported_cuda_avail is True and cuda_observed_available is False:
        hallucinations["capability"]["items"].append(
            {
                "type": "cuda_available_overclaim",
                "reported": True,
                "observed": False,
                "evidence": "build_output/cuda/results.json (or torch probe)",
            }
        )

    # gpu_count mismatch (only if both are known)
    reported_gpu_count = report.get("gpu_count")
    obs_gpu_count = observed.get("gpu_count")
    if isinstance(reported_gpu_count, int) and isinstance(obs_gpu_count, int):
        if reported_gpu_count != obs_gpu_count:
            hallucinations["capability"]["items"].append(
                {"type": "gpu_count_mismatch", "reported": reported_gpu_count, "observed": obs_gpu_count}
            )

    # ddp_expected_ok vs multi-gpu run (only judge if >=2 GPUs and stage not skipped and has outcome)
    ddp_expected_ok = report.get("ddp_expected_ok")
    if ddp_expected_ok is True:
        if isinstance(obs_gpu_count, int) and obs_gpu_count < 2:
            # inconclusive (not counted)
            log_lines.append("ddp_expected_ok=true but gpu_count<2 -> inconclusive")
        elif multi_status == "skipped":
            log_lines.append("multi_gpu stage skipped -> inconclusive for ddp_expected_ok")
        elif isinstance(obs_gpu_count, int) and obs_gpu_count >= 2:
            if multi_status in ("success", "failure") and multi_exit == 1:
                hallucinations["capability"]["items"].append(
                    {"type": "ddp_expected_ok_but_multi_gpu_failed", "reported": True, "observed_multi_gpu_exit_code": multi_exit}
                )
            elif multi_status not in ("success", "failure"):
                log_lines.append("multi_gpu stage missing/invalid -> inconclusive for ddp_expected_ok")

    hallucinations["capability"]["count"] = len(hallucinations["capability"]["items"])

    total_hallucinations = (
        hallucinations["path"]["count"] + hallucinations["version"]["count"] + hallucinations["capability"]["count"]
    )

    failure_category = "not_applicable"
    status = "success"
    exit_code = 0
    if total_hallucinations > 0:
        status = "failure"
        exit_code = 1
        if hallucinations["path"]["count"] > 0:
            failure_category = "path_hallucination"
        elif hallucinations["version"]["count"] > 0:
            failure_category = "version_hallucination"
        else:
            failure_category = "capability_hallucination"

    log_lines.append(f"status={status} total_hallucinations={total_hallucinations}")
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    write_json(
        results_path,
        {
            "status": status,
            "skip_reason": "not_applicable",
            "exit_code": exit_code,
            "stage": "hallucination",
            "task": "validate",
            "command": f"python {Path(__file__).name} --report-path {report_path}",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "meta": {
                "python": sys.executable,
                "git_commit": get_git_commit(REPO_ROOT),
                "env_vars": {k: os.environ.get(k, "") for k in ["SCIMLOPSBENCH_REPORT"] if os.environ.get(k)},
                "decision_reason": "Validated report fields against python subprocess probes and benchmark stage results.",
            },
            "report_path": str(report_path),
            "reported": report,
            "observed": observed,
            "hallucinations": hallucinations,
            "failure_category": failure_category,
            "error_excerpt": tail_lines(log_path),
        },
    )

    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
