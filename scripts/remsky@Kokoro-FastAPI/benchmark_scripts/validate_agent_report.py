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


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_report_path(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def get_git_commit(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def safe_load_json(path: Path) -> Tuple[Optional[dict], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, ""
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "unknown"


def run_python(python_path: str, code: str, timeout: int = 30) -> Tuple[bool, str]:
    try:
        out = subprocess.check_output([python_path, "-c", code], stderr=subprocess.STDOUT, text=True, timeout=timeout)
        return True, out.strip()
    except Exception as e:
        return False, str(e)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    logs: List[str] = []

    def log(msg: str) -> None:
        logs.append(msg)
        print(msg)

    report_path = resolve_report_path(args.report_path)
    report, report_err = safe_load_json(report_path)

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
        "multi_gpu_skip_reason": None,
        "notes": [],
    }

    status = "failure"
    exit_code = 1
    failure_category = "unknown"
    reported: Dict[str, Any] = {}

    if report is None:
        log(f"Report missing/invalid: {report_path} ({report_err})")
        failure_category = "missing_report" if report_err == "missing" else "invalid_json"
    else:
        reported = report
        python_path = report.get("python_path")
        reported_python_version = report.get("python_version")
        reported_torch_version = report.get("torch_version")
        reported_cuda_available = report.get("cuda_available")
        reported_gpu_count = report.get("gpu_count")
        reported_ddp_expected_ok = report.get("ddp_expected_ok")

        # Path hallucination checks
        if not isinstance(python_path, str) or not python_path.strip():
            hallucinations["path"]["items"].append({"type": "missing_python_path", "detail": "python_path missing/empty"})
        else:
            p = Path(python_path)
            if not p.exists():
                hallucinations["path"]["items"].append({"type": "python_path_not_found", "detail": python_path})
            elif not os.access(python_path, os.X_OK):
                hallucinations["path"]["items"].append({"type": "python_path_not_executable", "detail": python_path})
            else:
                ok_exe, exe = run_python(python_path, "import sys; print(sys.executable)")
                ok_ver, ver = run_python(python_path, "import platform; print(platform.python_version())")
                if not ok_exe or not ok_ver:
                    hallucinations["path"]["items"].append(
                        {
                            "type": "python_probe_failed",
                            "detail": {"python_path": python_path, "sys_executable_ok": ok_exe, "version_ok": ok_ver},
                        }
                    )
                else:
                    observed["python_path_ok"] = True
                    observed["python_executable"] = exe
                    observed["python_version"] = ver

        hallucinations["path"]["count"] = len(hallucinations["path"]["items"])

        # Version hallucination checks (only if python path is usable)
        if observed["python_path_ok"]:
            if isinstance(reported_python_version, str) and reported_python_version.strip():
                if reported_python_version.strip() != observed["python_version"]:
                    hallucinations["version"]["items"].append(
                        {
                            "type": "python_version_mismatch",
                            "reported": reported_python_version.strip(),
                            "observed": observed["python_version"],
                        }
                    )

            ok_torch, torch_ver_or_err = run_python(
                python_path, "import torch; print(getattr(torch, '__version__', ''))", timeout=60
            )
            if not ok_torch:
                hallucinations["version"]["items"].append(
                    {"type": "torch_import_failed", "detail": torch_ver_or_err}
                )
            else:
                observed["torch_import_ok"] = True
                observed["torch_version"] = torch_ver_or_err
                if isinstance(reported_torch_version, str) and reported_torch_version.strip():
                    if reported_torch_version.strip() != torch_ver_or_err.strip():
                        hallucinations["version"]["items"].append(
                            {
                                "type": "torch_version_mismatch",
                                "reported": reported_torch_version.strip(),
                                "observed": torch_ver_or_err.strip(),
                            }
                        )

        hallucinations["version"]["count"] = len(hallucinations["version"]["items"])

        # Capability hallucination checks (based on stage results, skipping inconclusive/skipped).
        cuda_results, _ = safe_load_json(root / "build_output" / "cuda" / "results.json")
        if isinstance(cuda_results, dict):
            obs = cuda_results.get("observed") or {}
            if isinstance(obs, dict) and "cuda_available" in obs:
                observed["cuda_available"] = bool(obs.get("cuda_available"))
                observed["gpu_count"] = int(obs.get("gpu_count") or 0)
        else:
            observed["notes"].append("cuda_stage_missing_or_invalid")

        single_results, _ = safe_load_json(root / "build_output" / "single_gpu" / "results.json")
        if isinstance(single_results, dict):
            observed["single_gpu_exit_code"] = int(single_results.get("exit_code") or 0)
        else:
            observed["notes"].append("single_gpu_stage_missing_or_invalid")

        multi_results, _ = safe_load_json(root / "build_output" / "multi_gpu" / "results.json")
        if isinstance(multi_results, dict):
            observed["multi_gpu_exit_code"] = int(multi_results.get("exit_code") or 0)
            observed["multi_gpu_status"] = multi_results.get("status")
            observed["multi_gpu_skip_reason"] = multi_results.get("skip_reason")
        else:
            observed["notes"].append("multi_gpu_stage_missing_or_invalid")

        # Only judge when we have observations.
        if isinstance(reported_cuda_available, bool) and observed["cuda_available"] is not None:
            if reported_cuda_available and (not bool(observed["cuda_available"])):
                hallucinations["capability"]["items"].append(
                    {
                        "type": "cuda_available_overclaim",
                        "reported": reported_cuda_available,
                        "observed": observed["cuda_available"],
                    }
                )

        if isinstance(reported_gpu_count, int) and observed["gpu_count"] is not None:
            if reported_gpu_count != int(observed["gpu_count"]):
                hallucinations["capability"]["items"].append(
                    {
                        "type": "gpu_count_mismatch",
                        "reported": reported_gpu_count,
                        "observed": int(observed["gpu_count"]),
                    }
                )

        # DDP expectation: only if >=2 GPUs AND multi-gpu stage was attempted (not skipped)
        if isinstance(reported_ddp_expected_ok, bool) and reported_ddp_expected_ok:
            if observed["gpu_count"] is None:
                observed["notes"].append("ddp_expected_ok_inconclusive_no_gpu_count")
            elif int(observed["gpu_count"]) < 2:
                observed["notes"].append("ddp_expected_ok_inconclusive_insufficient_gpus")
            else:
                # If stage skipped, do not count as hallucination.
                if observed["multi_gpu_status"] == "skipped":
                    observed["notes"].append("ddp_expected_ok_inconclusive_multi_gpu_skipped")
                elif observed["multi_gpu_exit_code"] is None:
                    observed["notes"].append("ddp_expected_ok_inconclusive_no_multi_gpu_result")
                elif int(observed["multi_gpu_exit_code"]) != 0:
                    hallucinations["capability"]["items"].append(
                        {
                            "type": "ddp_expected_ok_but_multi_gpu_failed",
                            "reported": True,
                            "observed_multi_gpu_exit_code": int(observed["multi_gpu_exit_code"]),
                            "observed_multi_gpu_status": observed["multi_gpu_status"],
                        }
                    )

        hallucinations["capability"]["count"] = len(hallucinations["capability"]["items"])

        total_h = sum(hallucinations[k]["count"] for k in ["path", "version", "capability"])
        if total_h == 0:
            status = "success"
            exit_code = 0
            failure_category = ""
        else:
            status = "failure"
            exit_code = 1
            if hallucinations["capability"]["count"] > 0:
                failure_category = "capability_hallucination"
            elif hallucinations["version"]["count"] > 0:
                failure_category = "version_hallucination"
            else:
                failure_category = "path_hallucination"

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"{sys.executable} {Path(__file__).name} --report-path {report_path}",
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
            "python": sys.executable,
            "git_commit": get_git_commit(root),
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
                "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            },
            "decision_reason": "Validate agent report fields against real probes and build_output stage observations.",
            "timestamp_utc": utc_timestamp(),
        },
        "failure_category": failure_category,
        "error_excerpt": "\n".join(logs[-200:]),
    }

    log_path.write_text("\n".join(logs) + ("\n" if logs else ""), encoding="utf-8")
    write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
