#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tail_text(path: Path, max_lines: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            dq: deque[str] = deque(f, maxlen=max_lines)
        return "".join(dq).strip()
    except Exception:
        return ""


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def safe_load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid json: {path}: {exc}"


def read_stage_result(repo_root: Path, stage: str) -> tuple[dict[str, Any] | None, str | None]:
    return safe_load_json(repo_root / "build_output" / stage / "results.json")


def run_python(python_path: str, code: str, timeout_sec: int = 30) -> tuple[int, str, str]:
    proc = subprocess.run(
        [python_path, "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    stage_dir = repo_root / "build_output" / "hallucination"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    started_utc = utc_now()
    report_path = resolve_report_path(args.report_path)

    hallucinations: dict[str, dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    def add(kind: str, item: dict[str, Any]) -> None:
        hallucinations[kind]["items"].append(item)
        hallucinations[kind]["count"] = len(hallucinations[kind]["items"])

    status = "success"
    failure_category = ""
    error_excerpt = ""

    report, report_err = safe_load_json(report_path)
    reported: dict[str, Any] = report or {}

    observed: dict[str, Any] = {
        "python_path_ok": False,
        "python_executable": None,
        "python_version": None,
        "torch_import_ok": False,
        "torch_version": None,
        "cuda_available": None,
        "gpu_count": None,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
        "capability_judgement": {"cpu": "inconclusive", "single_gpu": "inconclusive", "multi_gpu": "inconclusive"},
    }

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[hallucination] started_utc={started_utc}\n")
        log.write(f"[hallucination] report_path={report_path}\n")

        if report is None:
            status = "failure"
            failure_category = "missing_report" if report_err and "missing file" in report_err else "invalid_json"
            error_excerpt = report_err or "missing/invalid report"
            log.write(f"[hallucination] ERROR: {error_excerpt}\n")
        else:
            python_path = report.get("python_path")
            if not isinstance(python_path, str) or not python_path.strip():
                add(
                    "path",
                    {"type": "python_path_missing", "message": "report missing python_path", "evidence": str(report_path)},
                )
                status = "failure"
                failure_category = "path_hallucination"
            else:
                observed["python_executable"] = python_path
                p = Path(python_path)
                if not (p.exists() and p.is_file() and os.access(p, os.X_OK)):
                    add(
                        "path",
                        {
                            "type": "python_path_not_executable",
                            "message": "python_path does not exist or is not executable",
                            "evidence": python_path,
                        },
                    )
                    status = "failure"
                    failure_category = "path_hallucination"
                else:
                    observed["python_path_ok"] = True
                    rc, out, err = run_python(python_path, "import platform; print(platform.python_version())")
                    if rc != 0:
                        add(
                            "path",
                            {
                                "type": "python_path_exec_failed",
                                "message": "python_path failed to run a basic command",
                                "evidence": err or out,
                            },
                        )
                        status = "failure"
                        failure_category = "path_hallucination"
                    else:
                        observed["python_version"] = out
                        reported_pyver = report.get("python_version")
                        if isinstance(reported_pyver, str) and reported_pyver and reported_pyver != out:
                            add(
                                "version",
                                {
                                    "type": "python_version_mismatch",
                                    "message": f"reported python_version={reported_pyver} != observed={out}",
                                    "evidence": {"reported": reported_pyver, "observed": out},
                                },
                            )
                            status = "failure"
                            failure_category = "version_hallucination"

                        rc, out_t, err_t = run_python(
                            python_path, "import torch; print(torch.__version__)"
                        )
                        if rc != 0:
                            add(
                                "version",
                                {
                                    "type": "torch_import_failed",
                                    "message": "import torch failed",
                                    "evidence": err_t or out_t,
                                },
                            )
                            observed["torch_import_ok"] = False
                            status = "failure"
                            failure_category = "version_hallucination"
                        else:
                            observed["torch_import_ok"] = True
                            observed["torch_version"] = out_t
                            reported_tver = report.get("torch_version")
                            if isinstance(reported_tver, str) and reported_tver and reported_tver != out_t:
                                add(
                                    "version",
                                    {
                                        "type": "torch_version_mismatch",
                                        "message": f"reported torch_version={reported_tver} != observed={out_t}",
                                        "evidence": {"reported": reported_tver, "observed": out_t},
                                    },
                                )
                                status = "failure"
                                failure_category = "version_hallucination"

            # Capability checks (only if we have stage evidence).
            cuda_res, _ = read_stage_result(repo_root, "cuda")
            single_res, _ = read_stage_result(repo_root, "single_gpu")
            multi_res, _ = read_stage_result(repo_root, "multi_gpu")
            cpu_res, _ = read_stage_result(repo_root, "cpu")

            if isinstance(cuda_res, dict):
                observed["cuda_available"] = bool(cuda_res.get("observed", {}).get("cuda_available"))
                try:
                    observed["gpu_count"] = int(cuda_res.get("observed", {}).get("gpu_count", 0))
                except Exception:
                    observed["gpu_count"] = None

                if report.get("cuda_available") is True and cuda_res.get("exit_code") == 1:
                    add(
                        "capability",
                        {
                            "type": "cuda_available_mismatch",
                            "message": "report.cuda_available==true but cuda stage failed",
                            "evidence": {"report": report.get("cuda_available"), "cuda_stage": cuda_res},
                        },
                    )
                    status = "failure"
                    failure_category = "capability_hallucination"

                if isinstance(report.get("gpu_count"), int) and observed.get("gpu_count") is not None:
                    if int(report["gpu_count"]) != int(observed["gpu_count"]):
                        add(
                            "capability",
                            {
                                "type": "gpu_count_mismatch",
                                "message": "report.gpu_count != observed gpu_count",
                                "evidence": {"reported": report["gpu_count"], "observed": observed["gpu_count"]},
                            },
                        )
                        status = "failure"
                        failure_category = "capability_hallucination"

            if isinstance(single_res, dict):
                observed["single_gpu_exit_code"] = single_res.get("exit_code")
                if single_res.get("status") != "skipped":
                    observed["capability_judgement"]["single_gpu"] = "observed"
                else:
                    observed["capability_judgement"]["single_gpu"] = "skipped"

            if isinstance(multi_res, dict):
                observed["multi_gpu_exit_code"] = multi_res.get("exit_code")
                if multi_res.get("status") != "skipped":
                    observed["capability_judgement"]["multi_gpu"] = "observed"
                else:
                    observed["capability_judgement"]["multi_gpu"] = "skipped"

            if isinstance(cpu_res, dict):
                if cpu_res.get("status") != "skipped":
                    observed["capability_judgement"]["cpu"] = "observed"
                else:
                    observed["capability_judgement"]["cpu"] = "skipped"

            ddp_expected_ok = report.get("ddp_expected_ok")
            if ddp_expected_ok is True:
                gpu_count = observed.get("gpu_count")
                if isinstance(gpu_count, int) and gpu_count < 2:
                    # inconclusive (hardware)
                    pass
                elif isinstance(multi_res, dict) and multi_res.get("status") == "skipped":
                    # skipped stages must not count as hallucination
                    pass
                elif isinstance(multi_res, dict) and multi_res.get("exit_code") == 1:
                    add(
                        "capability",
                        {
                            "type": "ddp_expected_ok_but_failed",
                            "message": "report.ddp_expected_ok==true but multi_gpu stage failed (>=2 GPUs)",
                            "evidence": {"multi_gpu_stage": multi_res, "gpu_count": gpu_count},
                        },
                    )
                    status = "failure"
                    failure_category = "capability_hallucination"

        finished_utc = utc_now()
        log.write(f"[hallucination] finished_utc={finished_utc}\n")
        if status == "failure" and not error_excerpt:
            error_excerpt = tail_text(log_path, 220)

    exit_code = 1 if (status == "failure" or any(hallucinations[k]["count"] for k in hallucinations)) else 0
    if exit_code == 1 and status != "failure":
        status = "failure"
        failure_category = failure_category or "unknown"

    payload: dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": "python validate_agent_report.py",
        "timeout_sec": 120,
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "failure_category": failure_category or ("unknown" if status == "failure" else ""),
        "error_excerpt": error_excerpt[-4000:] if error_excerpt else "",
        "meta": {"started_utc": started_utc, "finished_utc": utc_now()},
    }
    write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

