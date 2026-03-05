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
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:  # noqa: BLE001
        return None, f"invalid_json: {e}"


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def is_executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except Exception:
        return False


def tail(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def load_stage_result(stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = repo_root() / "build_output" / stage / "results.json"
    data, err = read_json(path)
    if data is None:
        return None, f"{stage}:{err}"
    return data, None


def run_python_capture(python_executable: str, code: str, timeout: int = 30) -> Tuple[int, str, str]:
    cmd = [python_executable, "-c", code]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Validate agent report and compute hallucination statistics")
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args(argv)

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report, report_err = read_json(report_path)

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

    status = "failure"
    exit_code = 1
    failure_category = "unknown"

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[hallucination] start_utc={utc_timestamp()}\n")
        log_f.write(f"[hallucination] report_path={report_path}\n")

        if report is None:
            log_f.write(f"[hallucination] ERROR: report missing/invalid: {report_err}\n")
            failure_category = "missing_report" if report_err == "missing" else "invalid_json"
        else:
            reported_python_path = report.get("python_path")
            if not isinstance(reported_python_path, str) or not reported_python_path:
                hallucinations["path"]["items"].append(
                    {"type": "python_path_missing", "detail": "report.json missing python_path"}
                )
            else:
                py_path = Path(reported_python_path)
                observed["python_executable"] = reported_python_path
                if not is_executable(py_path):
                    hallucinations["path"]["items"].append(
                        {
                            "type": "python_path_not_executable",
                            "detail": f"python_path is not an executable file: {reported_python_path}",
                        }
                    )
                else:
                    rc, out, err = run_python_capture(
                        reported_python_path,
                        'import platform; print(platform.python_version())',
                        timeout=30,
                    )
                    if rc != 0:
                        hallucinations["path"]["items"].append(
                            {
                                "type": "python_path_exec_failed",
                                "detail": f"python -c failed rc={rc} stderr={err[:400]}",
                            }
                        )
                    else:
                        observed["python_path_ok"] = True
                        observed["python_version"] = out

            # Version checks (only if we can run python).
            if observed["python_path_ok"]:
                reported_py_ver = report.get("python_version")
                if isinstance(reported_py_ver, str) and reported_py_ver:
                    if observed["python_version"] and observed["python_version"] != reported_py_ver:
                        hallucinations["version"]["items"].append(
                            {
                                "type": "python_version_mismatch",
                                "detail": f"reported={reported_py_ver} observed={observed['python_version']}",
                            }
                        )

                reported_torch_ver = report.get("torch_version")
                if isinstance(reported_torch_ver, str) and reported_torch_ver:
                    rc, out, err = run_python_capture(
                        observed["python_executable"],
                        "import torch; print(torch.__version__)",
                        timeout=60,
                    )
                    if rc != 0:
                        hallucinations["version"]["items"].append(
                            {
                                "type": "torch_import_failed",
                                "detail": f"import torch failed rc={rc} stderr={err[:400]}",
                            }
                        )
                    else:
                        observed["torch_import_ok"] = True
                        observed["torch_version"] = out
                        if out != reported_torch_ver:
                            hallucinations["version"]["items"].append(
                                {
                                    "type": "torch_version_mismatch",
                                    "detail": f"reported={reported_torch_ver} observed={out}",
                                }
                            )

            # Observed capabilities come strictly from benchmark stage results.
            cuda_res, cuda_err = load_stage_result("cuda")
            single_res, single_err = load_stage_result("single_gpu")
            multi_res, multi_err = load_stage_result("multi_gpu")

            log_f.write(f"[hallucination] stage_load cuda={cuda_err or 'ok'}\n")
            log_f.write(f"[hallucination] stage_load single_gpu={single_err or 'ok'}\n")
            log_f.write(f"[hallucination] stage_load multi_gpu={multi_err or 'ok'}\n")

            # CUDA observations
            cuda_obs = None
            if isinstance(cuda_res, dict):
                cuda_obs = cuda_res.get("observed") if isinstance(cuda_res.get("observed"), dict) else None
            if isinstance(cuda_obs, dict) and "cuda_available" in cuda_obs:
                observed["cuda_available"] = bool(cuda_obs.get("cuda_available"))
                try:
                    observed["gpu_count"] = int(cuda_obs.get("gpu_count", 0))
                except Exception:
                    observed["gpu_count"] = None

            # Exit codes from entrypoint stages
            if isinstance(single_res, dict):
                observed["single_gpu_exit_code"] = int(single_res.get("exit_code", 1))
                single_status = str(single_res.get("status", "failure"))
            else:
                single_status = "missing"
            if isinstance(multi_res, dict):
                observed["multi_gpu_exit_code"] = int(multi_res.get("exit_code", 1))
                multi_status = str(multi_res.get("status", "failure"))
            else:
                multi_status = "missing"

            # Capability hallucinations: only judge when we have valid observations and stage isn't skipped.
            reported_cuda_avail = report.get("cuda_available")
            if isinstance(reported_cuda_avail, bool):
                if observed["cuda_available"] is not None:
                    if reported_cuda_avail and observed["cuda_available"] is False:
                        hallucinations["capability"]["items"].append(
                            {
                                "type": "cuda_available_mismatch",
                                "detail": "report.cuda_available==true but CUDA check observed false",
                            }
                        )

            reported_gpu_count = report.get("gpu_count")
            if isinstance(reported_gpu_count, int):
                if observed["gpu_count"] is not None:
                    if reported_gpu_count != observed["gpu_count"]:
                        hallucinations["capability"]["items"].append(
                            {
                                "type": "gpu_count_mismatch",
                                "detail": f"reported={reported_gpu_count} observed={observed['gpu_count']}",
                            }
                        )

            ddp_expected_ok = report.get("ddp_expected_ok")
            if isinstance(ddp_expected_ok, bool) and ddp_expected_ok:
                # Only judge if multi-gpu stage is included (not skipped) and we have >=2 GPUs observed.
                if multi_status == "skipped":
                    log_f.write("[hallucination] ddp_expected_ok: multi_gpu stage skipped -> inconclusive\n")
                elif isinstance(observed["gpu_count"], int) and observed["gpu_count"] < 2:
                    log_f.write("[hallucination] ddp_expected_ok: <2 GPUs observed -> inconclusive\n")
                elif isinstance(observed["gpu_count"], int) and observed["gpu_count"] >= 2:
                    if observed["multi_gpu_exit_code"] not in (0, None):
                        hallucinations["capability"]["items"].append(
                            {
                                "type": "ddp_expected_ok_but_failed",
                                "detail": "report.ddp_expected_ok==true, >=2 GPUs observed, but multi_gpu stage failed",
                            }
                        )

            # Finalize counts
            for k in ("path", "version", "capability"):
                hallucinations[k]["count"] = len(hallucinations[k]["items"])

            any_hallucination = any(hallucinations[k]["count"] > 0 for k in hallucinations)
            if any_hallucination:
                if hallucinations["path"]["count"] > 0:
                    failure_category = "path_hallucination"
                elif hallucinations["version"]["count"] > 0:
                    failure_category = "version_hallucination"
                else:
                    failure_category = "capability_hallucination"
                status = "failure"
                exit_code = 1
            else:
                status = "success"
                exit_code = 0
                failure_category = "unknown"

    payload: Dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "stage": "hallucination",
        "report_path": str(report_path),
        "reported": report if isinstance(report, dict) else {},
        "observed": observed,
        "hallucinations": hallucinations,
        "failure_category": failure_category,
        "error_excerpt": tail(log_path),
        "meta": {
            "timestamp_utc": utc_timestamp(),
        },
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

