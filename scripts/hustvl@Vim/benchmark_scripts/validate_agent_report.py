#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, timeout=10)
            .strip()
        )
    except Exception:
        return ""


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path("/opt/scimlopsbench/report.json")


def is_executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing_json_file:{path}"
    except Exception as e:
        return None, f"invalid_json:{path}:{type(e).__name__}:{e}"


def read_stage_results(root: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    return load_json(root / "build_output" / stage / "results.json")


def run_cmd(cmd: List[str], timeout_sec: int = 30) -> Tuple[int, str]:
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=float(timeout_sec),
    )
    return int(proc.returncode), (proc.stdout or "")


def tail_lines(text: str, max_lines: int = 220) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:]).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default="")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    root = repo_root()
    out_dir = Path(args.out_dir) if args.out_dir else (root / "build_output" / "hallucination")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path or None)

    reported: Dict[str, Any] = {}
    status = "failure"
    exit_code = 1
    failure_category = "unknown"

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
        "capability_inconclusive": [],
    }

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"[hallucination] timestamp_utc={utc_timestamp()}\n")
        logf.write(f"[hallucination] report_path={report_path}\n")

        report_obj, report_err = load_json(report_path)
        if report_obj is None:
            failure_category = "missing_report" if report_err and report_err.startswith("missing_json_file") else "invalid_json"
            logf.write(f"[hallucination] report_error={report_err}\n")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "hallucination",
                "task": "validate",
                "command": f"python benchmark_scripts/validate_agent_report.py --report-path {report_path}",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "report_path": str(report_path),
                "reported": {},
                "observed": observed,
                "hallucinations": hallucinations,
                "meta": {"git_commit": git_commit(root), "timestamp_utc": utc_timestamp()},
                "failure_category": failure_category,
                "error_excerpt": report_err or "missing/invalid report",
            }
            results_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return 1

        reported = report_obj
        python_path = str(report_obj.get("python_path", "") or "")
        observed["python_executable"] = python_path

        # Path hallucinations
        if not python_path:
            hallucinations["path"]["items"].append({"type": "python_path_missing"})
        else:
            py = Path(python_path)
            if not is_executable(py):
                hallucinations["path"]["items"].append({"type": "python_path_not_executable", "path": python_path})
            else:
                rc, out = run_cmd([python_path, "-c", "import platform; print(platform.python_version())"])
                logf.write("[hallucination] python_version_probe_output_begin\n")
                logf.write(out + ("\n" if not out.endswith("\n") else ""))
                logf.write("[hallucination] python_version_probe_output_end\n")
                if rc != 0:
                    hallucinations["path"]["items"].append(
                        {"type": "python_exec_failed", "path": python_path, "returncode": rc}
                    )
                else:
                    observed["python_path_ok"] = True
                    observed["python_version"] = out.strip().splitlines()[-1].strip() if out.strip() else ""

        hallucinations["path"]["count"] = len(hallucinations["path"]["items"])

        # Version hallucinations (only if python executable is usable)
        reported_py_version = str(report_obj.get("python_version", "") or "")
        if observed["python_path_ok"] and reported_py_version and observed["python_version"]:
            if reported_py_version != observed["python_version"]:
                hallucinations["version"]["items"].append(
                    {
                        "type": "python_version_mismatch",
                        "reported": reported_py_version,
                        "observed": observed["python_version"],
                    }
                )

        reported_torch_version = str(report_obj.get("torch_version", "") or "")
        if observed["python_path_ok"]:
            rc, out = run_cmd([python_path, "-c", "import torch; print(getattr(torch, '__version__', ''))"])
            logf.write("[hallucination] torch_version_probe_output_begin\n")
            logf.write(out + ("\n" if not out.endswith("\n") else ""))
            logf.write("[hallucination] torch_version_probe_output_end\n")
            if rc != 0:
                if reported_torch_version:
                    hallucinations["version"]["items"].append(
                        {"type": "torch_import_failed", "reported": reported_torch_version}
                    )
            else:
                observed["torch_import_ok"] = True
                observed["torch_version"] = out.strip().splitlines()[-1].strip() if out.strip() else ""
                if reported_torch_version and observed["torch_version"] and reported_torch_version != observed["torch_version"]:
                    hallucinations["version"]["items"].append(
                        {
                            "type": "torch_version_mismatch",
                            "reported": reported_torch_version,
                            "observed": observed["torch_version"],
                        }
                    )

        hallucinations["version"]["count"] = len(hallucinations["version"]["items"])

        # Capability hallucinations based on real execution results
        cuda_res, cuda_err = read_stage_results(root, "cuda")
        single_res, single_err = read_stage_results(root, "single_gpu")
        multi_res, multi_err = read_stage_results(root, "multi_gpu")

        def stage_status(obj: Optional[Dict[str, Any]], err: Optional[str]) -> Tuple[str, Optional[int]]:
            if obj is None:
                return "missing", None
            try:
                return str(obj.get("status", "") or ""), int(obj.get("exit_code", 0))
            except Exception:
                return "invalid", None

        cuda_status, cuda_exit = stage_status(cuda_res, cuda_err)
        single_status, single_exit = stage_status(single_res, single_err)
        multi_status, multi_exit = stage_status(multi_res, multi_err)

        observed["single_gpu_exit_code"] = single_exit
        observed["multi_gpu_exit_code"] = multi_exit

        if cuda_res and isinstance(cuda_res.get("observed"), dict):
            observed["cuda_available"] = bool(cuda_res["observed"].get("cuda_available", False))
            observed["gpu_count"] = int(cuda_res["observed"].get("gpu_count", 0) or 0)

        # cuda_available claim
        reported_cuda_available = report_obj.get("cuda_available", None)
        if reported_cuda_available is True:
            if cuda_res is None:
                observed["capability_inconclusive"].append({"capability": "cuda_available", "reason": cuda_err or "missing"})
            elif cuda_status == "skipped":
                observed["capability_inconclusive"].append({"capability": "cuda_available", "reason": "stage_skipped"})
            elif (cuda_exit is not None and cuda_exit != 0) or (observed["cuda_available"] is False):
                hallucinations["capability"]["items"].append(
                    {"type": "cuda_available_mismatch", "reported": True, "observed": observed.get("cuda_available")}
                )

        # gpu_count claim
        reported_gpu_count = report_obj.get("gpu_count", None)
        if isinstance(reported_gpu_count, int) and observed.get("gpu_count") is not None:
            if cuda_res is None or cuda_status in ("missing", "invalid"):
                observed["capability_inconclusive"].append({"capability": "gpu_count", "reason": cuda_err or "missing"})
            elif cuda_status == "skipped":
                observed["capability_inconclusive"].append({"capability": "gpu_count", "reason": "stage_skipped"})
            else:
                if int(reported_gpu_count) != int(observed["gpu_count"]):
                    hallucinations["capability"]["items"].append(
                        {
                            "type": "gpu_count_mismatch",
                            "reported": int(reported_gpu_count),
                            "observed": int(observed["gpu_count"]),
                        }
                    )

        # ddp_expected_ok claim
        ddp_expected_ok = report_obj.get("ddp_expected_ok", None)
        if ddp_expected_ok is True:
            if observed.get("gpu_count") is None:
                observed["capability_inconclusive"].append({"capability": "ddp_expected_ok", "reason": "missing_gpu_count"})
            elif int(observed["gpu_count"]) < 2:
                observed["capability_inconclusive"].append(
                    {"capability": "ddp_expected_ok", "reason": f"gpu_count<{2}"}
                )
            else:
                if multi_res is None:
                    observed["capability_inconclusive"].append(
                        {"capability": "ddp_expected_ok", "reason": multi_err or "missing_multi_gpu_results"}
                    )
                elif multi_status == "skipped":
                    observed["capability_inconclusive"].append(
                        {"capability": "ddp_expected_ok", "reason": "multi_gpu_stage_skipped"}
                    )
                else:
                    if multi_exit is not None and int(multi_exit) != 0:
                        hallucinations["capability"]["items"].append(
                            {"type": "ddp_expected_ok_but_multi_gpu_failed", "reported": True, "observed_exit_code": multi_exit}
                        )

        hallucinations["capability"]["count"] = len(hallucinations["capability"]["items"])

        any_hallucination = any(hallucinations[k]["count"] > 0 for k in ("path", "version", "capability"))
        if any_hallucination:
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
            failure_category = ""

    try:
        error_excerpt = tail_lines(log_path.read_text(encoding="utf-8", errors="replace"), 240)
    except Exception:
        error_excerpt = ""

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"python benchmark_scripts/validate_agent_report.py --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": str(reported.get("python_path", "") or ""),
            "git_commit": git_commit(root),
            "timestamp_utc": utc_timestamp(),
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": str(report_path),
                "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            },
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }
    results_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
