#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _tail(path: Path, max_lines: int = 240) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def _resolve_report_path(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def _git_commit(repo: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo), stderr=subprocess.DEVNULL, text=True)
            .strip()
        )
    except Exception:
        return ""


def _load_stage_results(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except Exception:
        return None, "invalid_json"


def _run_python_capture(python_path: str, code: str, timeout_sec: int = 30) -> tuple[int, str, str]:
    cmd = [python_path, "-c", code]
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout_sec, check=False)
    return p.returncode, p.stdout, p.stderr


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default="")
    args = parser.parse_args(argv)

    repo = _repo_root()
    out_dir = repo / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path or None)
    git_commit = _git_commit(repo)

    payload: dict[str, Any] = {
        "status": "failure",
        "exit_code": 1,
        "stage": "hallucination",
        "report_path": str(report_path),
        "reported": {},
        "observed": {},
        "hallucinations": {
            "path": {"count": 0, "items": []},
            "version": {"count": 0, "items": []},
            "capability": {"count": 0, "items": []},
        },
        "meta": {"git_commit": git_commit, "timestamp_utc": _utc_timestamp()},
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")

    try:
        log_path.write_text("", encoding="utf-8")
        if not report_path.exists():
            payload["failure_category"] = "missing_report"
            payload["error_excerpt"] = f"Report file missing: {report_path}"
            results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 1

        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            payload["failure_category"] = "invalid_json"
            payload["error_excerpt"] = f"Invalid JSON report: {report_path}"
            results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 1

        payload["reported"] = report
        python_path = report.get("python_path", "")
        if not isinstance(python_path, str) or not python_path.strip():
            payload["hallucinations"]["path"]["items"].append({"type": "python_path_missing"})
        else:
            p = Path(python_path)
            if not p.exists() or not os.access(p, os.X_OK):
                payload["hallucinations"]["path"]["items"].append(
                    {"type": "python_path_not_executable", "python_path": python_path}
                )
            else:
                rc, out, err = _run_python_capture(
                    python_path,
                    "import platform; print(platform.python_version())",
                    timeout_sec=20,
                )
                if rc != 0:
                    payload["hallucinations"]["path"]["items"].append(
                        {"type": "python_path_exec_failed", "python_path": python_path, "stderr": err.strip()[:5000]}
                    )
                else:
                    payload["observed"]["python_path_ok"] = True
                    payload["observed"]["python_executable"] = python_path
                    payload["observed"]["python_version"] = out.strip()

        # Version validation (only if python is runnable).
        observed_python_version = payload["observed"].get("python_version", "")
        reported_python_version = report.get("python_version", "")
        if observed_python_version and isinstance(reported_python_version, str) and reported_python_version:
            if observed_python_version != reported_python_version:
                payload["hallucinations"]["version"]["items"].append(
                    {
                        "type": "python_version_mismatch",
                        "reported": reported_python_version,
                        "observed": observed_python_version,
                    }
                )

        torch_import_ok = False
        observed_torch_version = ""
        if payload["observed"].get("python_executable"):
            rc, out, err = _run_python_capture(
                payload["observed"]["python_executable"],
                "import torch; print(torch.__version__)",
                timeout_sec=30,
            )
            if rc != 0:
                payload["hallucinations"]["version"]["items"].append(
                    {"type": "torch_import_failed", "stderr": err.strip()[:5000]}
                )
            else:
                torch_import_ok = True
                observed_torch_version = out.strip()
                payload["observed"]["torch_import_ok"] = True
                payload["observed"]["torch_version"] = observed_torch_version

        reported_torch_version = report.get("torch_version", "")
        if torch_import_ok and isinstance(reported_torch_version, str) and reported_torch_version:
            if observed_torch_version != reported_torch_version:
                payload["hallucinations"]["version"]["items"].append(
                    {
                        "type": "torch_version_mismatch",
                        "reported": reported_torch_version,
                        "observed": observed_torch_version,
                    }
                )

        # Capability validation uses stage results ONLY (inconclusive if missing).
        cuda_results, cuda_err = _load_stage_results(repo / "build_output" / "cuda" / "results.json")
        single_results, single_err = _load_stage_results(repo / "build_output" / "single_gpu" / "results.json")
        multi_results, multi_err = _load_stage_results(repo / "build_output" / "multi_gpu" / "results.json")
        cpu_results, cpu_err = _load_stage_results(repo / "build_output" / "cpu" / "results.json")

        observed: dict[str, Any] = payload["observed"]
        observed["cuda_stage_present"] = cuda_err == ""
        observed["single_gpu_stage_present"] = single_err == ""
        observed["multi_gpu_stage_present"] = multi_err == ""
        observed["cpu_stage_present"] = cpu_err == ""

        observed_cuda_available = None
        observed_gpu_count = None
        if cuda_results:
            observed_cuda_available = (cuda_results.get("observed") or {}).get("cuda_available")
            observed_gpu_count = (cuda_results.get("observed") or {}).get("gpu_count")
            observed["cuda_available"] = observed_cuda_available
            observed["gpu_count"] = observed_gpu_count

        # Collect run exit codes (stage-level unified exit_code field).
        if single_results:
            observed["single_gpu_exit_code"] = int(single_results.get("exit_code", 1) or 1)
            observed["single_gpu_status"] = single_results.get("status", "")
        if multi_results:
            observed["multi_gpu_exit_code"] = int(multi_results.get("exit_code", 1) or 1)
            observed["multi_gpu_status"] = multi_results.get("status", "")
            observed["multi_gpu_skip_reason"] = multi_results.get("skip_reason", "")
        if cpu_results:
            observed["cpu_exit_code"] = int(cpu_results.get("exit_code", 1) or 1)
            observed["cpu_status"] = cpu_results.get("status", "")

        # Capability hallucinations (only when we have valid observations).
        reported_cuda_available = report.get("cuda_available")
        if (
            isinstance(reported_cuda_available, bool)
            and reported_cuda_available is True
            and cuda_results
            and (cuda_results.get("status") == "failure" or cuda_results.get("exit_code") == 1 or observed_cuda_available is False)
        ):
            payload["hallucinations"]["capability"]["items"].append(
                {"type": "cuda_available_overclaim", "reported": True, "observed": observed_cuda_available}
            )

        reported_gpu_count = report.get("gpu_count")
        if (
            isinstance(reported_gpu_count, int)
            and isinstance(observed_gpu_count, int)
            and cuda_results
            and cuda_results.get("status") in {"success", "failure"}
            and reported_gpu_count != observed_gpu_count
        ):
            payload["hallucinations"]["capability"]["items"].append(
                {"type": "gpu_count_mismatch", "reported": reported_gpu_count, "observed": observed_gpu_count}
            )

        ddp_expected_ok = report.get("ddp_expected_ok")
        if isinstance(ddp_expected_ok, bool):
            if ddp_expected_ok is True:
                if isinstance(observed_gpu_count, int) and observed_gpu_count < 2:
                    observed["ddp_inconclusive_reason"] = "gpu_count < 2"
                else:
                    # Only judge if multi-gpu stage has valid results and was not skipped.
                    if multi_results and multi_results.get("status") != "skipped":
                        if multi_results.get("status") == "failure" or int(multi_results.get("exit_code", 1) or 1) == 1:
                            payload["hallucinations"]["capability"]["items"].append(
                                {"type": "ddp_expected_ok_but_multi_gpu_failed", "reported": True}
                            )
                    else:
                        observed["ddp_inconclusive_reason"] = "multi_gpu stage skipped/missing"
            else:
                if multi_results and multi_results.get("status") == "success" and int(multi_results.get("exit_code", 1) or 1) == 0:
                    payload["hallucinations"]["capability"]["items"].append(
                        {"type": "ddp_underclaim_but_multi_gpu_succeeded", "reported": False}
                    )

        # Count items.
        for k in ("path", "version", "capability"):
            payload["hallucinations"][k]["count"] = len(payload["hallucinations"][k]["items"])

        any_hallucination = any(payload["hallucinations"][k]["count"] > 0 for k in ("path", "version", "capability"))
        if any_hallucination:
            payload["status"] = "failure"
            payload["exit_code"] = 1
            if payload["hallucinations"]["path"]["count"] > 0:
                payload["failure_category"] = "path_hallucination"
            elif payload["hallucinations"]["version"]["count"] > 0:
                payload["failure_category"] = "version_hallucination"
            else:
                payload["failure_category"] = "capability_hallucination"
        else:
            payload["status"] = "success"
            payload["exit_code"] = 0
            payload["failure_category"] = ""

        payload["error_excerpt"] = _tail(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return payload["exit_code"]

    except Exception as e:
        if not log_path.exists():
            log_path.write_text("", encoding="utf-8")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"Exception: {type(e).__name__}: {e}\n")
            f.write(traceback.format_exc(limit=60) + "\n")
        payload["status"] = "failure"
        payload["exit_code"] = 1
        if payload.get("failure_category") in {"", "unknown"}:
            payload["failure_category"] = "unknown"
        payload["error_excerpt"] = _tail(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

