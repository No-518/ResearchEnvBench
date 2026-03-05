#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(txt.splitlines()[-max_lines:])
    except Exception:
        return ""

def empty_assets() -> Dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def load_assets(repo: Path) -> Dict[str, Any]:
    p = repo / "build_output" / "prepare" / "results.json"
    if not p.exists():
        return empty_assets()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        assets = d.get("assets")
        return assets if isinstance(assets, dict) else empty_assets()
    except Exception:
        return empty_assets()


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except Exception as e:
        return None, f"invalid_json: {e}"


def read_stage_result(repo: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], str]:
    return load_json(repo / "build_output" / stage / "results.json")


def run_python(python_exe: str, code: str, timeout_sec: int = 30) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            [python_exe, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
        )
        return int(proc.returncode), proc.stdout or ""
    except Exception as e:
        return 1, repr(e)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH))
    args = ap.parse_args(argv)

    repo = repo_root()
    out_dir = repo / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    status = "failure"
    exit_code = 1
    failure_category = "unknown"
    error_excerpt = ""
    assets = load_assets(repo)

    reported: Dict[str, Any] = {}
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

    hallucinations: Dict[str, Any] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    def add_item(kind: str, item: Dict[str, Any]) -> None:
        hallucinations[kind]["items"].append(item)
        hallucinations[kind]["count"] = len(hallucinations[kind]["items"])

    def write_results() -> None:
        payload = {
            "status": status,
            "skip_reason": "unknown",
            "exit_code": exit_code,
            "stage": "hallucination",
            "task": "validate",
            "command": f"python benchmark_scripts/validate_agent_report.py --report-path {args.report_path}",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": assets,
            "report_path": str(args.report_path),
            "reported": reported,
            "observed": observed,
            "hallucinations": hallucinations,
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
            "meta": {
                "python": sys.executable,
                "git_commit": "",
                "env_vars": {
                    "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
                    "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
                },
                "decision_reason": "Validate agent report claims vs observed stage outputs and direct python probes.",
                "timestamp_utc": utcnow(),
            },
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with log_path.open("w", encoding="utf-8") as log:
        try:
            report_path = Path(args.report_path)
            report_obj, err = load_json(report_path)
            if report_obj is None:
                failure_category = "missing_report" if err == "missing" else "invalid_json"
                error_excerpt = f"report.json read failed: {err}"
                log.write(error_excerpt + "\n")
                # missing/invalid report => failure
                status = "failure"
                exit_code = 1
                write_results()
                return 1

            reported = report_obj if isinstance(report_obj, dict) else {}

            # --- Path hallucinations ---
            python_path = reported.get("python_path")
            if not isinstance(python_path, str) or not python_path.strip():
                add_item("path", {"type": "python_path_missing", "message": "report.python_path missing/empty"})
            else:
                python_path = python_path.strip()
                observed["python_executable"] = python_path
                if not (Path(python_path).exists() and os.access(python_path, os.X_OK)):
                    add_item(
                        "path",
                        {"type": "python_path_not_executable", "python_path": python_path, "message": "not executable"},
                    )
                else:
                    rc, out = run_python(python_path, "import platform; print(platform.python_version())", timeout_sec=30)
                    if rc != 0:
                        add_item(
                            "path",
                            {
                                "type": "python_path_exec_failed",
                                "python_path": python_path,
                                "message": "python_path -c failed",
                                "output": out.strip()[-2000:],
                            },
                        )
                    else:
                        observed["python_path_ok"] = True
                        observed["python_version"] = (out.strip().splitlines()[-1] if out.strip() else "")

            # --- Version hallucinations ---
            reported_py_ver = reported.get("python_version")
            if isinstance(reported_py_ver, str) and observed.get("python_version"):
                if reported_py_ver.strip() != str(observed["python_version"]).strip():
                    add_item(
                        "version",
                        {
                            "type": "python_version_mismatch",
                            "reported": reported_py_ver.strip(),
                            "observed": str(observed["python_version"]).strip(),
                        },
                    )

            if isinstance(python_path, str) and observed.get("python_path_ok"):
                rc, out = run_python(python_path, "import torch; print(torch.__version__)", timeout_sec=30)
                if rc != 0:
                    observed["torch_import_ok"] = False
                    add_item(
                        "version",
                        {
                            "type": "torch_import_failed",
                            "message": "import torch failed in report python",
                            "output": out.strip()[-2000:],
                        },
                    )
                else:
                    observed["torch_import_ok"] = True
                    observed["torch_version"] = (out.strip().splitlines()[-1] if out.strip() else "")
                    reported_torch_ver = reported.get("torch_version")
                    if isinstance(reported_torch_ver, str) and reported_torch_ver.strip():
                        if reported_torch_ver.strip() != str(observed["torch_version"]).strip():
                            add_item(
                                "version",
                                {
                                    "type": "torch_version_mismatch",
                                    "reported": reported_torch_ver.strip(),
                                    "observed": str(observed["torch_version"]).strip(),
                                },
                            )

            # --- Observed capabilities (from benchmark stage results) ---
            cuda_res, cuda_err = read_stage_result(repo, "cuda")
            if isinstance(cuda_res, dict):
                cuda_obs = cuda_res.get("observed")
                if isinstance(cuda_obs, dict):
                    if isinstance(cuda_obs.get("cuda_available"), bool):
                        observed["cuda_available"] = cuda_obs["cuda_available"]
                    if isinstance(cuda_obs.get("gpu_count"), int):
                        observed["gpu_count"] = cuda_obs["gpu_count"]
                if observed["cuda_available"] is None and isinstance(cuda_res.get("exit_code"), int):
                    observed["cuda_available"] = bool(cuda_res.get("exit_code") == 0)
            else:
                log.write(f"[validate] cuda stage results unavailable: {cuda_err}\n")

            single_res, single_err = read_stage_result(repo, "single_gpu")
            if isinstance(single_res, dict) and isinstance(single_res.get("exit_code"), int):
                observed["single_gpu_exit_code"] = int(single_res["exit_code"])
            else:
                log.write(f"[validate] single_gpu stage results unavailable: {single_err}\n")

            multi_res, multi_err = read_stage_result(repo, "multi_gpu")
            multi_status = None
            if isinstance(multi_res, dict):
                multi_status = multi_res.get("status")
                if isinstance(multi_res.get("exit_code"), int):
                    observed["multi_gpu_exit_code"] = int(multi_res["exit_code"])
            else:
                log.write(f"[validate] multi_gpu stage results unavailable: {multi_err}\n")

            # --- Capability hallucinations (only when we have valid observations) ---
            reported_cuda = reported.get("cuda_available")
            if reported_cuda is True and isinstance(observed.get("cuda_available"), bool):
                if observed["cuda_available"] is False:
                    add_item(
                        "capability",
                        {
                            "type": "cuda_available_overclaim",
                            "message": "report.cuda_available=true but cuda stage observed false",
                        },
                    )

            reported_gpu_count = reported.get("gpu_count")
            if isinstance(reported_gpu_count, int) and isinstance(observed.get("gpu_count"), int):
                if reported_gpu_count != observed["gpu_count"]:
                    add_item(
                        "capability",
                        {
                            "type": "gpu_count_mismatch",
                            "reported": reported_gpu_count,
                            "observed": observed["gpu_count"],
                        },
                    )

            ddp_expected_ok = reported.get("ddp_expected_ok")
            if ddp_expected_ok is True:
                # Only judge if we know gpu_count.
                if isinstance(observed.get("gpu_count"), int):
                    if observed["gpu_count"] < 2:
                        # Inconclusive per spec (<2 GPUs).
                        log.write("[validate] ddp_expected_ok inconclusive: <2 GPUs observed\n")
                    elif multi_status == "skipped":
                        # Skipped stages are excluded from capability hallucination.
                        log.write("[validate] ddp_expected_ok inconclusive: multi_gpu stage skipped\n")
                    elif isinstance(observed.get("multi_gpu_exit_code"), int):
                        if observed["multi_gpu_exit_code"] != 0:
                            add_item(
                                "capability",
                                {
                                    "type": "ddp_expected_ok_but_failed",
                                    "message": "ddp_expected_ok=true, >=2 GPUs observed, but multi_gpu stage failed",
                                },
                            )
                    else:
                        log.write("[validate] ddp_expected_ok inconclusive: multi_gpu exit_code missing\n")

            # Final classification: any hallucination => failure.
            any_hallucination = any(hallucinations[k]["count"] > 0 for k in ("path", "version", "capability"))
            status = "failure" if any_hallucination else "success"
            exit_code = 1 if any_hallucination else 0

            if hallucinations["path"]["count"] > 0:
                failure_category = "path_hallucination"
            elif hallucinations["version"]["count"] > 0:
                failure_category = "version_hallucination"
            elif hallucinations["capability"]["count"] > 0:
                failure_category = "capability_hallucination"
            else:
                failure_category = "unknown"

            error_excerpt = tail_lines(log_path, 220) if status == "failure" else ""
        except Exception:
            status = "failure"
            exit_code = 1
            failure_category = "unknown"
            tb = traceback.format_exc()
            log.write(tb + "\n")
            error_excerpt = "\n".join(tb.splitlines()[-220:])
        finally:
            write_results()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
