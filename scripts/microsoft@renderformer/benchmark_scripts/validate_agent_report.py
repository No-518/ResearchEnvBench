#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
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


def _tail(path: Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-n:])


def _safe_json_load(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except Exception as e:
        return None, f"invalid json: {path}: {e}"


def _is_executable(path: str) -> bool:
    try:
        p = Path(path)
        if not p.is_file():
            return False
        mode = p.stat().st_mode
        return bool(mode & stat.S_IXUSR) and os.access(str(p), os.X_OK)
    except Exception:
        return False


def _run_python(python_path: str, code: str, timeout_sec: int = 30) -> Tuple[int, str, str]:
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


def _read_stage_result(repo_root: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = repo_root / "build_output" / stage / "results.json"
    data, err = _safe_json_load(path)
    return data, err


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate agent report.json and compute hallucination statistics.")
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = args.report_path or os.environ.get("SCIMLOPSBENCH_REPORT") or DEFAULT_REPORT_PATH

    logs: list[str] = []
    logs.append(f"[hallucination] timestamp_utc={_utc_timestamp()}")
    logs.append(f"[hallucination] report_path={report_path}")

    hallucinations: Dict[str, Dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    def add(kind: str, item: Dict[str, Any]) -> None:
        hallucinations[kind]["items"].append(item)
        hallucinations[kind]["count"] = len(hallucinations[kind]["items"])

    reported: Dict[str, Any] = {}
    report_data, report_err = _safe_json_load(Path(report_path))
    if report_data is None:
        logs.append(f"[hallucination] report_error={report_err}")
        add("path", {"type": "missing_report", "message": str(report_err or "missing report")})
    else:
        reported = report_data

    python_path_ok = False
    python_executable = ""
    observed_python_version = ""

    if reported:
        python_executable = str(reported.get("python_path") or "")
        if not python_executable:
            add("path", {"type": "python_path_missing", "message": "report.python_path is missing"})
        elif not _is_executable(python_executable):
            add("path", {"type": "python_path_not_executable", "message": f"python_path not executable: {python_executable}"})
        else:
            rc, out, err = _run_python(python_executable, "import platform; print(platform.python_version())")
            if rc != 0 or not out:
                add(
                    "path",
                    {
                        "type": "python_path_unusable",
                        "message": f"failed to run python_path ({python_executable})",
                        "stderr": err,
                    },
                )
            else:
                python_path_ok = True
                observed_python_version = out.strip()

    torch_import_ok = False
    observed_torch_version = ""
    if python_path_ok:
        rc, out, err = _run_python(python_executable, "import torch; print(torch.__version__)")
        if rc == 0 and out:
            torch_import_ok = True
            observed_torch_version = out.strip()
        else:
            torch_import_ok = False
            observed_torch_version = ""
            if "torch_version" in reported:
                add(
                    "version",
                    {
                        "type": "torch_import_failed",
                        "message": "report.torch_version provided but import torch failed",
                        "stderr": err,
                    },
                )

    if python_path_ok and isinstance(reported.get("python_version"), str) and reported.get("python_version"):
        reported_py_ver = str(reported.get("python_version") or "")
        if observed_python_version and reported_py_ver != observed_python_version:
            add(
                "version",
                {
                    "type": "python_version_mismatch",
                    "message": f"reported python_version={reported_py_ver} != observed={observed_python_version}",
                },
            )

    if torch_import_ok and isinstance(reported.get("torch_version"), str) and reported.get("torch_version"):
        reported_torch_ver = str(reported.get("torch_version") or "")
        if observed_torch_version and reported_torch_ver != observed_torch_version:
            add(
                "version",
                {
                    "type": "torch_version_mismatch",
                    "message": f"reported torch_version={reported_torch_ver} != observed={observed_torch_version}",
                },
            )

    cuda_res, cuda_err = _read_stage_result(repo_root, "cuda")
    single_res, single_err = _read_stage_result(repo_root, "single_gpu")
    multi_res, multi_err = _read_stage_result(repo_root, "multi_gpu")

    observed_cuda_available: Optional[bool] = None
    observed_gpu_count: Optional[int] = None

    if isinstance(cuda_res, dict):
        observed_cuda_available = bool(cuda_res.get("observed", {}).get("cuda_available")) if isinstance(cuda_res.get("observed"), dict) else None
        if observed_cuda_available is None:
            observed_cuda_available = bool(cuda_res.get("status") == "success" and int(cuda_res.get("exit_code", 1)) == 0)
        if isinstance(cuda_res.get("observed"), dict) and isinstance(cuda_res["observed"].get("gpu_count"), int):
            observed_gpu_count = int(cuda_res["observed"]["gpu_count"])
    else:
        logs.append(f"[hallucination] cuda_results_error={cuda_err}")

    single_exit: Optional[int] = None
    single_status: str = ""
    if isinstance(single_res, dict):
        single_exit = int(single_res.get("exit_code", 1))
        single_status = str(single_res.get("status", ""))
    else:
        logs.append(f"[hallucination] single_gpu_results_error={single_err}")

    multi_exit: Optional[int] = None
    multi_status: str = ""
    if isinstance(multi_res, dict):
        multi_exit = int(multi_res.get("exit_code", 1))
        multi_status = str(multi_res.get("status", ""))
    else:
        logs.append(f"[hallucination] multi_gpu_results_error={multi_err}")

    if observed_cuda_available is not None and isinstance(reported.get("cuda_available"), bool):
        if reported.get("cuda_available") is True and observed_cuda_available is False:
            add(
                "capability",
                {
                    "type": "cuda_available_mismatch",
                    "message": "report.cuda_available=true but CUDA check failed",
                    "observed": observed_cuda_available,
                },
            )

    if observed_gpu_count is not None and isinstance(reported.get("gpu_count"), int):
        if int(reported.get("gpu_count")) != int(observed_gpu_count):
            add(
                "capability",
                {
                    "type": "gpu_count_mismatch",
                    "message": f"reported gpu_count={reported.get('gpu_count')} != observed={observed_gpu_count}",
                },
            )

    ddp_expected_ok = reported.get("ddp_expected_ok")
    if isinstance(ddp_expected_ok, bool) and ddp_expected_ok is True:
        if observed_gpu_count is not None and observed_gpu_count < 2:
            logs.append("[hallucination] ddp_expected_ok=true but <2 GPUs observed -> inconclusive")
        else:
            if multi_status == "skipped":
                logs.append("[hallucination] multi_gpu stage skipped -> inconclusive for ddp")
            elif multi_exit is not None and multi_exit != 0:
                add(
                    "capability",
                    {
                        "type": "ddp_expected_ok_but_failed",
                        "message": "report.ddp_expected_ok=true but multi-GPU stage failed",
                        "multi_gpu_exit_code": multi_exit,
                    },
                )

    logs.append(f"[hallucination] path_count={hallucinations['path']['count']}")
    logs.append(f"[hallucination] version_count={hallucinations['version']['count']}")
    logs.append(f"[hallucination] capability_count={hallucinations['capability']['count']}")

    any_hallucination = any(hallucinations[k]["count"] > 0 for k in ["path", "version", "capability"])
    status = "failure" if any_hallucination or report_data is None else "success"
    exit_code = 1 if status == "failure" else 0

    failure_category = "unknown"
    if report_data is None:
        if report_err and "invalid json" in report_err:
            failure_category = "invalid_json"
        else:
            failure_category = "missing_report"
    elif hallucinations["path"]["count"] > 0:
        failure_category = "path_hallucination"
    elif hallucinations["version"]["count"] > 0:
        failure_category = "version_hallucination"
    elif hallucinations["capability"]["count"] > 0:
        failure_category = "capability_hallucination"

    log_text = "\n".join(logs) + "\n"
    log_path.write_text(log_text, encoding="utf-8")

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"{sys.executable} benchmark_scripts/validate_agent_report.py --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "report_path": report_path,
        "reported": reported,
        "observed": {
            "python_path_ok": python_path_ok,
            "python_executable": python_executable,
            "python_version": observed_python_version,
            "torch_import_ok": torch_import_ok,
            "torch_version": observed_torch_version,
            "cuda_available": observed_cuda_available,
            "gpu_count": observed_gpu_count,
            "single_gpu_exit_code": single_exit,
            "multi_gpu_exit_code": multi_exit,
            "single_gpu_status": single_status,
            "multi_gpu_status": multi_status,
        },
        "hallucinations": hallucinations,
        "meta": {
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_timestamp(),
        },
        "failure_category": failure_category,
        "error_excerpt": _tail(log_path, n=220),
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

