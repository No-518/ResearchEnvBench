#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


def tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def is_executable_file(path: Path) -> bool:
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode):
            return False
        return os.access(path, os.X_OK)
    except Exception:
        return False


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, f"missing file: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, f"not an object: {path}"
        return data, None
    except Exception as e:
        return None, f"invalid json: {path}: {e}"


def run_python(python_path: str, code: str, timeout_sec: int = 30) -> Tuple[bool, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        out = subprocess.check_output(
            [python_path, "-c", code],
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            timeout=timeout_sec,
        ).strip()
        return True, out
    except Exception as e:
        msg = getattr(e, "output", "") or str(e)
        return False, str(msg).strip()


def stage_result_path(root: Path, stage: str) -> Path:
    return root / "build_output" / stage / "results.json"


def load_stage_results(root: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], str]:
    p = stage_result_path(root, stage)
    data, err = read_json(p)
    if err:
        return None, err
    return data, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate agent report and compute hallucination statistics.")
    parser.add_argument("--report-path", default="", help="Override report.json path.")
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = Path(
        args.report_path
        or os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH)
    )
    timeout_sec = 120
    command = " ".join(
        shlex.quote(x)
        for x in [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    )

    reported: Dict[str, Any] = {}
    observed: Dict[str, Any] = {
        "python_path_ok": False,
        "python_executable": "",
        "python_version": "",
        "torch_import_ok": False,
        "torch_version": "",
        "cuda_available": None,
        "gpu_count": None,
        "cpu_exit_code": None,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
    }

    hallucinations: Dict[str, Dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    status = "failure"
    exit_code = 1
    failure_category = "unknown"

    def add_item(kind: str, item: Dict[str, Any]) -> None:
        hallucinations[kind]["items"].append(item)
        hallucinations[kind]["count"] = len(hallucinations[kind]["items"])

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"[hallucination] timestamp_utc={utc_timestamp()}\n")
        logf.write(f"[hallucination] report_path={report_path}\n")
        logf.write(f"[hallucination] command={command}\n")

        report, report_err = read_json(report_path)
        if report_err:
            logf.write(f"[hallucination] ERROR: {report_err}\n")
            failure_category = "missing_report" if "missing file" in report_err else "invalid_json"
            status = "failure"
            exit_code = 1
        else:
            reported = report or {}
            python_path = str(reported.get("python_path", "")).strip()
            observed["python_executable"] = python_path

            if not python_path:
                add_item("path", {"type": "python_path_missing", "detail": "report.json missing python_path"})
            else:
                p = Path(python_path)
                if not p.exists():
                    add_item("path", {"type": "python_path_not_found", "detail": f"{python_path} does not exist"})
                elif not is_executable_file(p):
                    add_item("path", {"type": "python_path_not_executable", "detail": f"{python_path} not executable"})
                else:
                    ok, out = run_python(python_path, "import platform; print(platform.python_version())")
                    if not ok:
                        add_item("path", {"type": "python_invocation_failed", "detail": out})
                    else:
                        observed["python_path_ok"] = True
                        observed["python_version"] = out.strip()

            # Version checks (only if python path works).
            if observed["python_path_ok"]:
                reported_py_ver = str(reported.get("python_version", "")).strip()
                if reported_py_ver and reported_py_ver != observed["python_version"]:
                    add_item(
                        "version",
                        {
                            "type": "python_version_mismatch",
                            "reported": reported_py_ver,
                            "observed": observed["python_version"],
                        },
                    )

                reported_torch_ver = str(reported.get("torch_version", "")).strip()
                ok, out = run_python(
                    observed["python_executable"],
                    "import torch; print(torch.__version__)",
                    timeout_sec=60,
                )
                if ok:
                    observed["torch_import_ok"] = True
                    observed["torch_version"] = out.strip()
                    if reported_torch_ver and reported_torch_ver != observed["torch_version"]:
                        add_item(
                            "version",
                            {
                                "type": "torch_version_mismatch",
                                "reported": reported_torch_ver,
                                "observed": observed["torch_version"],
                            },
                        )
                else:
                    observed["torch_import_ok"] = False
                    if reported_torch_ver:
                        add_item(
                            "version",
                            {"type": "torch_import_failed", "reported": reported_torch_ver, "detail": out},
                        )

            # Capability checks: only judge when observations exist and stage wasn't skipped.
            cuda_stage, cuda_err = load_stage_results(root, "cuda")
            if cuda_stage:
                observed["cuda_available"] = bool(cuda_stage.get("exit_code", 1) == 0)
                gpu_count = cuda_stage.get("observed", {}).get("gpu_count", None)
                if isinstance(gpu_count, int):
                    observed["gpu_count"] = gpu_count
                elif isinstance(gpu_count, str) and gpu_count.isdigit():
                    observed["gpu_count"] = int(gpu_count)
            else:
                logf.write(f"[hallucination] WARNING: cuda stage unavailable: {cuda_err}\n")

            def get_exit_code(stage: str) -> Tuple[Optional[int], str]:
                d, err = load_stage_results(root, stage)
                if not d:
                    return None, err
                st = d.get("status", "")
                if st == "skipped":
                    return None, "skipped"
                ec = d.get("exit_code", None)
                if isinstance(ec, int):
                    return ec, ""
                return None, "missing exit_code"

            cpu_ec, cpu_note = get_exit_code("cpu")
            sg_ec, sg_note = get_exit_code("single_gpu")
            mg_ec, mg_note = get_exit_code("multi_gpu")
            observed["cpu_exit_code"] = cpu_ec
            observed["single_gpu_exit_code"] = sg_ec
            observed["multi_gpu_exit_code"] = mg_ec

            if cpu_note:
                logf.write(f"[hallucination] cpu stage note: {cpu_note}\n")
            if sg_note:
                logf.write(f"[hallucination] single_gpu stage note: {sg_note}\n")
            if mg_note:
                logf.write(f"[hallucination] multi_gpu stage note: {mg_note}\n")

            # report.cuda_available
            rep_cuda = reported.get("cuda_available", None)
            if isinstance(rep_cuda, bool) and (observed["cuda_available"] is not None):
                if rep_cuda and not bool(observed["cuda_available"]):
                    add_item(
                        "capability",
                        {
                            "type": "cuda_available_overclaim",
                            "reported": rep_cuda,
                            "observed": observed["cuda_available"],
                            "evidence": "build_output/cuda/results.json",
                        },
                    )

            # report.gpu_count
            rep_gc = reported.get("gpu_count", None)
            if isinstance(rep_gc, int) and isinstance(observed["gpu_count"], int):
                if rep_gc != observed["gpu_count"]:
                    add_item(
                        "capability",
                        {
                            "type": "gpu_count_mismatch",
                            "reported": rep_gc,
                            "observed": observed["gpu_count"],
                            "evidence": "build_output/cuda/results.json",
                        },
                    )

            # report.ddp_expected_ok
            rep_ddp = reported.get("ddp_expected_ok", None)
            if isinstance(rep_ddp, bool):
                if rep_ddp:
                    if isinstance(observed["gpu_count"], int) and observed["gpu_count"] >= 2:
                        if mg_ec is None:
                            logf.write("[hallucination] multi_gpu run skipped/inconclusive; not judging ddp_expected_ok\n")
                        else:
                            if mg_ec != 0:
                                add_item(
                                    "capability",
                                    {
                                        "type": "ddp_expected_ok_but_multi_failed",
                                        "reported": True,
                                        "observed_multi_gpu_exit_code": mg_ec,
                                        "evidence": "build_output/multi_gpu/results.json",
                                    },
                                )
                    else:
                        logf.write("[hallucination] <2 GPUs observed; ddp_expected_ok is inconclusive\n")
                else:
                    # Optional: if ddp_expected_ok == False but multi succeeded, flag as underclaim.
                    if mg_ec == 0:
                        add_item(
                            "capability",
                            {
                                "type": "ddp_underclaim",
                                "reported": False,
                                "observed_multi_gpu_exit_code": 0,
                                "evidence": "build_output/multi_gpu/results.json",
                            },
                        )

            # Determine overall outcome.
            if report_err:
                status = "failure"
                exit_code = 1
            else:
                any_h = any(hallucinations[k]["count"] > 0 for k in hallucinations)
                status = "failure" if any_h else "success"
                exit_code = 1 if any_h else 0

            if hallucinations["capability"]["count"] > 0:
                failure_category = "capability_hallucination"
            elif hallucinations["version"]["count"] > 0:
                failure_category = "version_hallucination"
            elif hallucinations["path"]["count"] > 0:
                failure_category = "path_hallucination"
            else:
                failure_category = "unknown"

        # Write a structured summary to the log.
        logf.write(f"[hallucination] status={status} exit_code={exit_code} failure_category={failure_category}\n")
        logf.write(f"[hallucination] counts={{path:{hallucinations['path']['count']}, version:{hallucinations['version']['count']}, capability:{hallucinations['capability']['count']}}}\n")

    results: Dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
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
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": sys.executable,
            "git_commit": git_commit(root),
            "env_vars": {
                k: os.environ.get(k, "")
                for k in [
                    "CUDA_VISIBLE_DEVICES",
                    "HF_HOME",
                    "TRANSFORMERS_CACHE",
                    "HF_DATASETS_CACHE",
                    "PIP_CACHE_DIR",
                    "XDG_CACHE_HOME",
                    "SENTENCE_TRANSFORMERS_HOME",
                    "TORCH_HOME",
                    "PYTHONDONTWRITEBYTECODE",
                    "SCIMLOPSBENCH_REPORT",
                    "SCIMLOPSBENCH_PYTHON",
                ]
            },
            "decision_reason": "Validate report.json python_path and versions by executing the reported python, then compare against observed benchmark stage results; do not judge capabilities for stages marked skipped.",
            "timestamp_utc": utc_timestamp(),
        },
        "failure_category": failure_category,
        "error_excerpt": tail_text(log_path),
    }

    results_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
