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
from typing import Any, Dict, List, Optional, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_report_path(cli_path: Optional[str]) -> Path:
    if cli_path:
        return Path(cli_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def safe_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def tail_excerpt(path: Path, max_lines: int = 220) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:])
    except Exception:
        return ""


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json: {path}: {e}"
    except Exception as e:
        return None, f"failed reading json: {path}: {e}"


def is_executable(path: Path) -> bool:
    return path.is_file() and os.access(str(path), os.X_OK)


def run_python(python_exe: str, code: str, timeout_sec: int = 20) -> Tuple[int, str, str]:
    p = subprocess.run(
        [python_exe, "-c", code],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_sec,
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def read_stage_results(stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = repo_root() / "build_output" / stage / "results.json"
    return load_json(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None, help="Override report path (highest priority)")
    args = ap.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    started = datetime.now(tz=timezone.utc)
    report_path = resolve_report_path(args.report_path)

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
        "multi_gpu_status": None,
        "cuda_stage_status": None,
    }

    reported: Dict[str, Any] = {}
    status = "failure"
    exit_code = 1
    failure_category = "unknown"

    def add_item(kind: str, item: Dict[str, Any]) -> None:
        hallucinations[kind]["items"].append(item)
        hallucinations[kind]["count"] = len(hallucinations[kind]["items"])

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write("== validate_agent_report stage ==\n")
        log_f.write(f"report_path: {report_path}\n")

        report, rep_err = load_json(report_path)
        if report is None:
            log_f.write((rep_err or "missing report") + "\n")
            failure_category = "missing_report"
        else:
            reported = dict(report)
            python_path = report.get("python_path")
            reported_pyver = report.get("python_version")
            reported_torchver = report.get("torch_version")
            reported_cuda_avail = report.get("cuda_available")
            reported_gpu_count = report.get("gpu_count")
            reported_ddp_ok = report.get("ddp_expected_ok")

            # Path hallucinations
            if not isinstance(python_path, str) or not python_path:
                add_item("path", {"issue": "python_path_missing", "reported": python_path})
                failure_category = "path_hallucination"
            else:
                py = Path(python_path)
                if not is_executable(py):
                    add_item(
                        "path",
                        {
                            "issue": "python_path_not_executable",
                            "reported": python_path,
                            "exists": py.exists(),
                        },
                    )
                    failure_category = "path_hallucination"
                else:
                    observed["python_path_ok"] = True
                    observed["python_executable"] = python_path
                    rc, out, err = run_python(python_path, "import platform; print(platform.python_version())")
                    if rc != 0:
                        add_item(
                            "path",
                            {"issue": "python_invocation_failed", "reported": python_path, "stderr": err[-1000:]},
                        )
                        failure_category = "path_hallucination"
                    else:
                        observed["python_version"] = out

                        # Version hallucinations (python)
                        if isinstance(reported_pyver, str) and reported_pyver and out and reported_pyver != out:
                            add_item(
                                "version",
                                {"issue": "python_version_mismatch", "reported": reported_pyver, "observed": out},
                            )
                            failure_category = failure_category if failure_category != "unknown" else "version_hallucination"

                        # Version hallucinations (torch)
                        if isinstance(reported_torchver, str) and reported_torchver:
                            rc2, out2, err2 = run_python(
                                python_path, "import torch; print(torch.__version__)"
                            )
                            if rc2 != 0:
                                add_item(
                                    "version",
                                    {
                                        "issue": "torch_import_failed",
                                        "reported": reported_torchver,
                                        "stderr": err2[-1000:],
                                    },
                                )
                                observed["torch_import_ok"] = False
                                failure_category = failure_category if failure_category != "unknown" else "version_hallucination"
                            else:
                                observed["torch_import_ok"] = True
                                observed["torch_version"] = out2
                                if out2 and reported_torchver != out2:
                                    add_item(
                                        "version",
                                        {
                                            "issue": "torch_version_mismatch",
                                            "reported": reported_torchver,
                                            "observed": out2,
                                        },
                                    )
                                    failure_category = failure_category if failure_category != "unknown" else "version_hallucination"

            # Capability hallucinations (based on stage results when available)
            cuda_res, cuda_err = read_stage_results("cuda")
            if cuda_res is not None:
                observed["cuda_stage_status"] = cuda_res.get("status")
                obs = cuda_res.get("observed") or {}
                if isinstance(obs, dict):
                    if "cuda_available" in obs:
                        observed["cuda_available"] = obs.get("cuda_available")
                    if "gpu_count" in obs:
                        observed["gpu_count"] = obs.get("gpu_count")
            else:
                log_f.write(f"cuda stage results unavailable: {cuda_err}\n")

            single_res, _ = read_stage_results("single_gpu")
            if single_res is not None:
                observed["single_gpu_exit_code"] = single_res.get("exit_code")

            multi_res, multi_err = read_stage_results("multi_gpu")
            if multi_res is not None:
                observed["multi_gpu_exit_code"] = multi_res.get("exit_code")
                observed["multi_gpu_status"] = multi_res.get("status")
            else:
                log_f.write(f"multi_gpu stage results unavailable: {multi_err}\n")

            # cuda_available mismatch
            if isinstance(reported_cuda_avail, bool) and observed["cuda_available"] is not None:
                if reported_cuda_avail is True and bool(observed["cuda_available"]) is False:
                    add_item(
                        "capability",
                        {
                            "issue": "cuda_available_overclaimed",
                            "reported": True,
                            "observed": bool(observed["cuda_available"]),
                            "evidence": "build_output/cuda/results.json",
                        },
                    )
                    failure_category = "capability_hallucination"

            # gpu_count mismatch
            if isinstance(reported_gpu_count, int) and observed["gpu_count"] is not None:
                try:
                    obs_gc = int(observed["gpu_count"])
                except Exception:
                    obs_gc = None
                if obs_gc is not None and reported_gpu_count != obs_gc:
                    add_item(
                        "capability",
                        {
                            "issue": "gpu_count_mismatch",
                            "reported": reported_gpu_count,
                            "observed": obs_gc,
                            "evidence": "build_output/cuda/results.json",
                        },
                    )
                    failure_category = "capability_hallucination"

            # ddp_expected_ok
            if isinstance(reported_ddp_ok, bool) and reported_ddp_ok is True:
                obs_gc = observed["gpu_count"]
                if isinstance(obs_gc, int) and obs_gc >= 2:
                    if observed["multi_gpu_status"] == "skipped":
                        log_f.write("ddp_expected_ok check inconclusive: multi_gpu stage skipped\n")
                    elif observed["multi_gpu_exit_code"] is None:
                        log_f.write("ddp_expected_ok check inconclusive: multi_gpu stage missing results\n")
                    else:
                        if int(observed["multi_gpu_exit_code"]) != 0:
                            add_item(
                                "capability",
                                {
                                    "issue": "ddp_expected_ok_but_multi_gpu_failed",
                                    "reported": True,
                                    "observed_multi_gpu_exit_code": observed["multi_gpu_exit_code"],
                                    "evidence": "build_output/multi_gpu/results.json",
                                },
                            )
                            failure_category = "capability_hallucination"
                else:
                    log_f.write("ddp_expected_ok check inconclusive: <2 GPUs observed\n")

    # Determine final status/exit code
    path_count = int(hallucinations["path"]["count"])
    ver_count = int(hallucinations["version"]["count"])
    cap_count = int(hallucinations["capability"]["count"])
    any_h = (path_count + ver_count + cap_count) > 0

    if any_h:
        status = "failure"
        exit_code = 1
        if path_count > 0:
            failure_category = "path_hallucination"
        elif ver_count > 0:
            failure_category = "version_hallucination"
        else:
            failure_category = "capability_hallucination"
    else:
        # Missing/invalid report is already a failure.
        if failure_category in {"missing_report", "invalid_json", "path_hallucination"}:
            status = "failure"
            exit_code = 1
        else:
            status = "success"
            exit_code = 0
            failure_category = "unknown"

    ended = datetime.now(tz=timezone.utc)

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": int(exit_code),
        "stage": "hallucination",
        "task": "validate",
        "command": f"{sys.executable} benchmark_scripts/validate_agent_report.py --report-path {str(report_path)}",
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
            "timestamp_utc": started.isoformat(),
            "start_time_utc": started.isoformat(),
            "end_time_utc": ended.isoformat(),
            "duration_sec": max(0.0, (ended - started).total_seconds()),
            "note": "Capability checks rely on build_output/<stage>/results.json; skipped stages are treated as inconclusive and do not count as hallucination.",
        },
        "failure_category": failure_category,
        "error_excerpt": tail_excerpt(log_path),
    }

    safe_write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        root = repo_root()
        out_dir = root / "build_output" / "hallucination"
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "log.txt"
        results_path = out_dir / "results.json"
        with log_path.open("a", encoding="utf-8") as f:
            f.write("fatal exception\n")
            f.write(traceback.format_exc() + "\n")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "hallucination",
            "task": "validate",
            "command": f"{sys.executable} benchmark_scripts/validate_agent_report.py",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}},
            "report_path": "",
            "reported": {},
            "observed": {},
            "hallucinations": {"path": {"count": 0, "items": []}, "version": {"count": 0, "items": []}, "capability": {"count": 0, "items": []}},
            "meta": {"timestamp_utc": datetime.now(tz=timezone.utc).isoformat()},
            "failure_category": "unknown",
            "error_excerpt": tail_excerpt(log_path),
        }
        safe_write_json(results_path, payload)
        raise SystemExit(1)

