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


def _read_stage_results(repo_root: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    p = repo_root / "build_output" / stage / "results.json"
    data, err = _safe_json_load(p)
    if err is not None:
        return None, err
    if not isinstance(data, dict):
        return None, "invalid_json: not an object"
    return data, None


def _run_python(python_exe: str, code: str, *, timeout_sec: int = 30) -> Tuple[int, str, str]:
    proc = subprocess.run(
        [python_exe, "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_sec,
        env=os.environ.copy(),
    )
    return int(proc.returncode), proc.stdout or "", proc.stderr or ""


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate /opt/scimlopsbench/report.json and compute hallucination stats.")
    parser.add_argument("--report-path", default=None, help="Override report.json path (highest priority).")
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "hallucination"
    _ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)
    command_str = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).name))} --report-path {shlex.quote(report_path)}"

    status = "failure"
    exit_code = 1
    failure_category = "unknown"
    error_excerpt = ""

    hallucinations: Dict[str, Any] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": [], "inconclusive": []},
    }

    reported: Dict[str, Any] = {}
    observed: Dict[str, Any] = {
        "python_path_ok": False,
        "python_executable": "",
        "python_version": "",
        "torch_import_ok": False,
        "torch_version": "",
        "cuda_available": None,
        "gpu_count": None,
        "cpu_status": None,
        "cpu_exit_code": None,
        "single_gpu_status": None,
        "single_gpu_exit_code": None,
        "multi_gpu_status": None,
        "multi_gpu_exit_code": None,
    }

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[hallucination] report_path={report_path}\n")
        log_f.write(f"[hallucination] timestamp_utc={_utc_timestamp()}\n")
        log_f.flush()

        report_file = Path(report_path)
        report_json, report_err = _safe_json_load(report_file)
        if report_err is not None:
            failure_category = "missing_report" if report_err == "missing" else "invalid_json"
            log_f.write(f"[hallucination] report error: {report_err}\n")
        else:
            assert isinstance(report_json, dict)
            reported = report_json
            python_path = str(report_json.get("python_path") or "")
            observed["python_executable"] = python_path

            if not python_path:
                hallucinations["path"]["items"].append(
                    {
                        "type": "path",
                        "field": "python_path",
                        "message": "python_path missing from report.json",
                    }
                )
            elif not _is_executable_file(python_path):
                hallucinations["path"]["items"].append(
                    {
                        "type": "path",
                        "field": "python_path",
                        "message": f"python_path is not an executable file: {python_path}",
                    }
                )
            else:
                rc, out, err = _run_python(
                    python_path,
                    "import platform; print(platform.python_version())",
                    timeout_sec=20,
                )
                log_f.write(f"[hallucination] python_version_probe_rc={rc}\n")
                if err:
                    log_f.write("[hallucination] python_version_probe_stderr:\n")
                    log_f.write(err + ("\n" if not err.endswith("\n") else ""))
                if rc != 0:
                    hallucinations["path"]["items"].append(
                        {
                            "type": "path",
                            "field": "python_path",
                            "message": "python_path exists but cannot execute version probe",
                        }
                    )
                else:
                    observed["python_path_ok"] = True
                    observed["python_version"] = out.strip()

            # Version checks
            reported_py_ver = report_json.get("python_version")
            if isinstance(reported_py_ver, str) and observed["python_version"]:
                if reported_py_ver.strip() != observed["python_version"].strip():
                    hallucinations["version"]["items"].append(
                        {
                            "type": "version",
                            "field": "python_version",
                            "reported": reported_py_ver.strip(),
                            "observed": observed["python_version"].strip(),
                        }
                    )

            reported_torch_ver = report_json.get("torch_version")
            if observed["python_path_ok"]:
                rc, out, err = _run_python(
                    python_path,
                    "import torch; print(getattr(torch, '__version__', ''))",
                    timeout_sec=30,
                )
                log_f.write(f"[hallucination] torch_version_probe_rc={rc}\n")
                if err:
                    log_f.write("[hallucination] torch_version_probe_stderr:\n")
                    log_f.write(err + ("\n" if not err.endswith("\n") else ""))
                if rc == 0:
                    observed["torch_import_ok"] = True
                    observed["torch_version"] = out.strip()
                else:
                    observed["torch_import_ok"] = False
                    if isinstance(reported_torch_ver, str) and reported_torch_ver.strip():
                        hallucinations["version"]["items"].append(
                            {
                                "type": "version",
                                "field": "torch_version",
                                "message": "reported torch_version but import torch failed",
                                "reported": reported_torch_ver.strip(),
                            }
                        )

            if isinstance(reported_torch_ver, str) and reported_torch_ver.strip() and observed["torch_import_ok"]:
                if reported_torch_ver.strip() != observed["torch_version"].strip():
                    hallucinations["version"]["items"].append(
                        {
                            "type": "version",
                            "field": "torch_version",
                            "reported": reported_torch_ver.strip(),
                            "observed": observed["torch_version"].strip(),
                        }
                    )

            # Observed capabilities: read benchmark stage results
            cuda_res, cuda_err = _read_stage_results(repo_root, "cuda")
            if cuda_res and isinstance(cuda_res.get("observed"), dict):
                o = cuda_res["observed"]
                if isinstance(o.get("cuda_available"), bool):
                    observed["cuda_available"] = bool(o["cuda_available"])
                if isinstance(o.get("gpu_count"), int):
                    observed["gpu_count"] = int(o["gpu_count"])

            for st in ["cpu", "single_gpu", "multi_gpu"]:
                res, _err = _read_stage_results(repo_root, st)
                if res:
                    observed[f"{st}_status"] = res.get("status")
                    observed[f"{st}_exit_code"] = res.get("exit_code")

            # Capability hallucinations (only when observations are available / applicable)
            reported_cuda_avail = report_json.get("cuda_available")
            if isinstance(reported_cuda_avail, bool) and observed["cuda_available"] is not None:
                if reported_cuda_avail and observed["cuda_available"] is False:
                    hallucinations["capability"]["items"].append(
                        {
                            "type": "capability",
                            "field": "cuda_available",
                            "reported": True,
                            "observed": False,
                            "evidence": "build_output/cuda/results.json",
                        }
                    )
            elif isinstance(reported_cuda_avail, bool) and observed["cuda_available"] is None:
                hallucinations["capability"]["inconclusive"].append(
                    {
                        "field": "cuda_available",
                        "reason": "missing or invalid cuda stage observation",
                    }
                )

            reported_gpu_count = report_json.get("gpu_count")
            if isinstance(reported_gpu_count, int) and observed["gpu_count"] is not None:
                if int(reported_gpu_count) != int(observed["gpu_count"]):
                    hallucinations["capability"]["items"].append(
                        {
                            "type": "capability",
                            "field": "gpu_count",
                            "reported": int(reported_gpu_count),
                            "observed": int(observed["gpu_count"]),
                            "evidence": "build_output/cuda/results.json",
                        }
                    )
            elif isinstance(reported_gpu_count, int) and observed["gpu_count"] is None:
                hallucinations["capability"]["inconclusive"].append(
                    {"field": "gpu_count", "reason": "missing or invalid cuda stage observation"}
                )

            ddp_expected_ok = report_json.get("ddp_expected_ok")
            multi_res, multi_err = _read_stage_results(repo_root, "multi_gpu")
            if isinstance(ddp_expected_ok, bool) and ddp_expected_ok is True:
                if observed["gpu_count"] is None:
                    hallucinations["capability"]["inconclusive"].append(
                        {"field": "ddp_expected_ok", "reason": "gpu_count unknown"}
                    )
                elif int(observed["gpu_count"]) < 2:
                    hallucinations["capability"]["inconclusive"].append(
                        {"field": "ddp_expected_ok", "reason": "gpu_count < 2"}
                    )
                elif multi_res is None:
                    hallucinations["capability"]["inconclusive"].append(
                        {"field": "ddp_expected_ok", "reason": f"multi_gpu results missing/invalid: {multi_err}"}
                    )
                else:
                    if multi_res.get("status") == "skipped":
                        hallucinations["capability"]["inconclusive"].append(
                            {"field": "ddp_expected_ok", "reason": "multi_gpu stage skipped"}
                        )
                    elif int(multi_res.get("exit_code") or 0) != 0 or multi_res.get("status") == "failure":
                        hallucinations["capability"]["items"].append(
                            {
                                "type": "capability",
                                "field": "ddp_expected_ok",
                                "reported": True,
                                "observed": False,
                                "evidence": "multi_gpu stage failed with >=2 GPUs visible",
                            }
                        )

            # Stage-skipped capability exclusions (record but do not count)
            cpu_res, _ = _read_stage_results(repo_root, "cpu")
            if cpu_res and cpu_res.get("status") == "skipped":
                hallucinations["capability"]["inconclusive"].append(
                    {"field": "cpu", "reason": "cpu stage skipped (repo_not_supported)"}
                )

    hallucinations["path"]["count"] = len(hallucinations["path"]["items"])
    hallucinations["version"]["count"] = len(hallucinations["version"]["items"])
    hallucinations["capability"]["count"] = len(hallucinations["capability"]["items"])

    any_hallucination = (
        hallucinations["path"]["count"] > 0
        or hallucinations["version"]["count"] > 0
        or hallucinations["capability"]["count"] > 0
    )

    if report_err is not None:
        status = "failure"
        exit_code = 1
        if failure_category == "unknown":
            failure_category = "missing_report" if report_err == "missing" else "invalid_json"
    elif any_hallucination:
        status = "failure"
        exit_code = 1
        if hallucinations["capability"]["count"] > 0:
            failure_category = "capability_hallucination"
        elif hallucinations["version"]["count"] > 0:
            failure_category = "version_hallucination"
        else:
            failure_category = "path_hallucination"
    else:
        status = "success"
        exit_code = 0
        failure_category = "unknown"

    error_excerpt = _tail_lines(log_path) if status == "failure" else ""

    payload: Dict[str, Any] = {
        "status": status,
        "exit_code": int(exit_code),
        "stage": "hallucination",
        "task": "validate",
        "command": command_str,
        "timeout_sec": 120,
        "framework": "unknown",
        "report_path": report_path,
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_timestamp(),
        },
        "failure_category": failure_category if status == "failure" else "unknown",
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())

