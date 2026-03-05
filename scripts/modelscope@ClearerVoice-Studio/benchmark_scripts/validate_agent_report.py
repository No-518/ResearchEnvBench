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


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail(path: Path, n: int = 240) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:
        return None, f"invalid_json: {e}"


def resolve_report_path(cli: str) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT", "").strip()
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def run_cmd(cmd: List[str], timeout_sec: int, log_path: Path) -> Tuple[int, str]:
    with log_path.open("a", encoding="utf-8") as log_fp:
        log_fp.write(f"[hallucination] cmd={cmd}\n")
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=timeout_sec)
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(out)
        return 0, out.strip()
    except subprocess.CalledProcessError as e:
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(e.output or "")
        return int(e.returncode or 1), (e.output or "").strip()
    except Exception as e:
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(f"[hallucination] exception: {type(e).__name__}: {e}\n")
        return 1, ""


def stage_result(stage: str, root: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    path = root / "build_output" / stage / "results.json"
    data, err = read_json(path)
    if data is None:
        return None, err or "missing"
    return data, ""


def normalize_stage_outcome(data: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[int]]:
    if not data:
        return None, None
    status = str(data.get("status") or "")
    try:
        exit_code = int(data.get("exit_code"))
    except Exception:
        exit_code = None
    return status or None, exit_code


def add_item(bucket: Dict[str, Any], kind: str, item: Dict[str, Any]) -> None:
    bucket[kind]["items"].append(item)
    bucket[kind]["count"] = int(bucket[kind]["count"]) + 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate agent report and compute hallucination statistics.")
    ap.add_argument("--report-path", default="", help="Override report.json path")
    args = ap.parse_args()

    root = repo_root()
    stage_dir = root / "build_output" / "hallucination"
    ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    report_path = resolve_report_path(args.report_path)
    report, report_err = read_json(report_path)

    command = f"python {Path(__file__).name} --report-path {report_path}" if args.report_path else f"python {Path(__file__).name}"
    timeout_sec = 120
    git_commit = ""
    try:
        git_commit = (
            subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True)
            .strip()
        )
    except Exception:
        git_commit = ""

    hallucinations: Dict[str, Any] = {
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
        "single_gpu_status": None,
        "multi_gpu_status": None,
        "cuda_stage_exit_code": None,
        "cuda_stage_status": None,
    }

    failure_category = "none"
    status = "success"
    exit_code = 0

    if report is None:
        failure_category = "missing_report" if report_err == "missing" else "invalid_json"
        status = "failure"
        exit_code = 1
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(f"[hallucination] report read failed: {report_err} ({report_path})\n")
        payload = {
            "status": status,
            "exit_code": exit_code,
            "stage": "hallucination",
            "task": "validate",
            "command": command,
            "timeout_sec": timeout_sec,
            "framework": "unknown",
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "skip_reason": "unknown",
            "report_path": str(report_path),
            "reported": {},
            "observed": observed,
            "hallucinations": hallucinations,
            "meta": {
                "python": sys.executable,
                "git_commit": git_commit,
                "env_vars": {"SCIMLOPSBENCH_REPORT": str(report_path)},
                "decision_reason": "Report missing/invalid; cannot validate agent claims.",
                "timestamp_utc": utc_ts(),
            },
            "failure_category": failure_category,
            "error_excerpt": tail(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    reported = dict(report)
    python_path = str(reported.get("python_path") or "").strip()
    reported_py_ver = str(reported.get("python_version") or "").strip()
    reported_torch_ver = str(reported.get("torch_version") or "").strip()
    reported_cuda_avail = reported.get("cuda_available", None)
    reported_gpu_count = reported.get("gpu_count", None)
    reported_ddp_ok = reported.get("ddp_expected_ok", None)

    with log_path.open("a", encoding="utf-8") as log_fp:
        log_fp.write(f"[hallucination] report_path={report_path}\n")
        log_fp.write(f"[hallucination] reported.python_path={python_path}\n")

    # Path hallucinations.
    if not python_path:
        add_item(hallucinations, "path", {"type": "python_path_missing", "detail": "python_path missing in report.json"})
    else:
        py = Path(python_path)
        if not (py.exists() and os.access(str(py), os.X_OK) and py.is_file()):
            add_item(hallucinations, "path", {"type": "python_path_not_executable", "detail": python_path})
        else:
            observed["python_executable"] = python_path
            rc, out = run_cmd([python_path, "-c", "import platform; print(platform.python_version())"], timeout_sec=30, log_path=log_path)
            if rc != 0 or not out:
                add_item(
                    hallucinations,
                    "path",
                    {"type": "python_invocation_failed", "detail": f"exit_code={rc}"},
                )
            else:
                observed["python_path_ok"] = True
                observed["python_version"] = out.strip().splitlines()[-1].strip()

    # Version hallucinations.
    if observed["python_path_ok"] and reported_py_ver and observed["python_version"] and (reported_py_ver != observed["python_version"]):
        add_item(
            hallucinations,
            "version",
            {"type": "python_version_mismatch", "reported": reported_py_ver, "observed": observed["python_version"]},
        )

    if observed["python_path_ok"]:
        rc, out = run_cmd([python_path, "-c", "import torch; print(torch.__version__)"], timeout_sec=60, log_path=log_path)
        if rc != 0 or not out:
            add_item(hallucinations, "version", {"type": "torch_import_failed", "detail": f"exit_code={rc}"})
        else:
            observed["torch_import_ok"] = True
            observed["torch_version"] = out.strip().splitlines()[-1].strip()
            if reported_torch_ver and observed["torch_version"] and (reported_torch_ver != observed["torch_version"]):
                add_item(
                    hallucinations,
                    "version",
                    {"type": "torch_version_mismatch", "reported": reported_torch_ver, "observed": observed["torch_version"]},
                )

    # Observed GPU count via torch (best-effort).
    if observed["python_path_ok"] and observed["torch_import_ok"]:
        rc, out = run_cmd(
            [
                python_path,
                "-c",
                "import torch; print(int(torch.cuda.is_available())); print(torch.cuda.device_count())",
            ],
            timeout_sec=30,
            log_path=log_path,
        )
        if rc == 0 and out:
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if len(lines) >= 2:
                observed["cuda_available"] = lines[-2] == "1"
                try:
                    observed["gpu_count"] = int(lines[-1])
                except Exception:
                    observed["gpu_count"] = None

    # Stage evidence.
    cuda_res, _ = stage_result("cuda", root)
    single_res, _ = stage_result("single_gpu", root)
    multi_res, _ = stage_result("multi_gpu", root)

    cuda_status, cuda_exit = normalize_stage_outcome(cuda_res)
    single_status, single_exit = normalize_stage_outcome(single_res)
    multi_status, multi_exit = normalize_stage_outcome(multi_res)
    observed["cuda_stage_status"] = cuda_status
    observed["cuda_stage_exit_code"] = cuda_exit
    observed["single_gpu_status"] = single_status
    observed["single_gpu_exit_code"] = single_exit
    observed["multi_gpu_status"] = multi_status
    observed["multi_gpu_exit_code"] = multi_exit

    # Capability hallucinations (only when observations are conclusive).
    if reported_gpu_count is not None and observed["gpu_count"] is not None:
        try:
            if int(reported_gpu_count) != int(observed["gpu_count"]):
                add_item(
                    hallucinations,
                    "capability",
                    {
                        "type": "gpu_count_mismatch",
                        "reported": int(reported_gpu_count),
                        "observed": int(observed["gpu_count"]),
                    },
                )
        except Exception:
            pass

    # report.cuda_available==true but cuda stage failed => capability hallucination.
    if reported_cuda_avail is True:
        if cuda_status == "skipped":
            pass
        elif cuda_exit is None:
            pass
        elif int(cuda_exit) != 0:
            add_item(
                hallucinations,
                "capability",
                {"type": "reported_cuda_available_but_probe_failed", "detail": f"cuda_exit_code={cuda_exit}"},
            )

    # ddp_expected_ok==true and >=2 GPUs but multi-GPU run failed => capability hallucination.
    if reported_ddp_ok is True:
        if multi_status == "skipped":
            pass
        elif observed["gpu_count"] is None:
            pass
        elif int(observed["gpu_count"]) < 2:
            pass
        else:
            if multi_exit is None:
                pass
            elif int(multi_exit) != 0:
                add_item(
                    hallucinations,
                    "capability",
                    {"type": "ddp_expected_ok_but_multi_gpu_failed", "detail": f"multi_gpu_exit_code={multi_exit}"},
                )

    # Determine overall status/failure category.
    total_hallucinations = (
        hallucinations["path"]["count"] + hallucinations["version"]["count"] + hallucinations["capability"]["count"]
    )
    if total_hallucinations > 0:
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
        failure_category = "none"

    payload = {
        "status": status,
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": command,
        "timeout_sec": timeout_sec,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "skip_reason": "unknown",
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": sys.executable,
            "git_commit": git_commit,
            "env_vars": {"SCIMLOPSBENCH_REPORT": str(report_path)},
            "decision_reason": "Compared report.json claims against runtime probes and benchmark stage results.",
            "timestamp_utc": utc_ts(),
        },
        "failure_category": failure_category,
        "error_excerpt": tail(log_path) if status != "success" else "",
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
