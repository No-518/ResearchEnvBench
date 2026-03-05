#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(DEFAULT_REPORT_PATH)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:]).strip()
    except Exception:
        return ""


def read_json_file(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError as e:
        return None, f"invalid_json: {e}"
    except Exception as e:
        return None, f"error: {e}"


def run_py(python_path: str, code: str, timeout_sec: int = 30) -> Tuple[int, str, str]:
    proc = subprocess.run([python_path, "-c", code], capture_output=True, text=True, timeout=timeout_sec)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    root = repo_root()
    stage_dir = root / "build_output" / "hallucination"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    report_path = resolve_report_path(args.report_path)

    path_items: List[Dict[str, Any]] = []
    version_items: List[Dict[str, Any]] = []
    capability_items: List[Dict[str, Any]] = []

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

    failure_category = "unknown"

    with log_path.open("w", encoding="utf-8") as log_f:
        def log(msg: str) -> None:
            log_f.write(msg.rstrip() + "\n")
            log_f.flush()

        log(f"[hallucination] repo_root={root}")
        log(f"[hallucination] report_path={report_path}")

        report_data, report_err = read_json_file(report_path)
        if report_data is None:
            failure_category = "missing_report" if report_err == "missing" else "invalid_json"
            log(f"[hallucination] ERROR: cannot read report.json: {report_err}")
            reported = {}
        else:
            reported = dict(report_data)

        python_path = str(reported.get("python_path") or "")
        if not python_path:
            path_items.append({"type": "python_path_missing", "message": "report.python_path is missing/empty"})
            failure_category = "missing_report" if failure_category == "unknown" else failure_category
        else:
            py = Path(python_path)
            if not py.exists():
                path_items.append({"type": "python_path_not_found", "message": f"python_path does not exist: {python_path}"})
            elif not os.access(python_path, os.X_OK):
                path_items.append(
                    {"type": "python_path_not_executable", "message": f"python_path not executable: {python_path}"}
                )
            else:
                observed["python_path_ok"] = True
                observed["python_executable"] = python_path
                rc, out, err = run_py(python_path, "import platform; print(platform.python_version())", timeout_sec=20)
                if rc != 0:
                    path_items.append(
                        {
                            "type": "python_version_probe_failed",
                            "message": f"python_path -c probe failed (rc={rc})",
                            "stderr": err,
                        }
                    )
                else:
                    observed["python_version"] = out

        # Version checks
        if observed.get("python_path_ok"):
            reported_py_ver = str(reported.get("python_version") or "")
            if reported_py_ver and observed["python_version"] and reported_py_ver != observed["python_version"]:
                version_items.append(
                    {
                        "type": "python_version_mismatch",
                        "reported": reported_py_ver,
                        "observed": observed["python_version"],
                    }
                )

            # Torch version
            rc, out, err = run_py(
                python_path,
                "import torch; print(getattr(torch,'__version__',''))",
                timeout_sec=40,
            )
            if rc != 0:
                version_items.append(
                    {
                        "type": "torch_import_failed",
                        "message": "import torch failed in reported python environment",
                        "stderr": err,
                    }
                )
            else:
                observed["torch_import_ok"] = True
                observed["torch_version"] = out
                reported_torch_ver = str(reported.get("torch_version") or "")
                if reported_torch_ver and out and reported_torch_ver != out:
                    version_items.append(
                        {
                            "type": "torch_version_mismatch",
                            "reported": reported_torch_ver,
                            "observed": out,
                        }
                    )

        # Capability checks from stage results (only when observations exist and are not skipped).
        cuda_res_path = root / "build_output" / "cuda" / "results.json"
        single_res_path = root / "build_output" / "single_gpu" / "results.json"
        multi_res_path = root / "build_output" / "multi_gpu" / "results.json"

        cuda_res, cuda_err = read_json_file(cuda_res_path)
        single_res, single_err = read_json_file(single_res_path)
        multi_res, multi_err = read_json_file(multi_res_path)

        if single_res and isinstance(single_res, dict):
            observed["single_gpu_exit_code"] = int(single_res.get("exit_code", 1))
        if multi_res and isinstance(multi_res, dict):
            observed["multi_gpu_exit_code"] = int(multi_res.get("exit_code", 1))

        cuda_available_obs: Optional[bool] = None
        gpu_count_obs: Optional[int] = None
        if cuda_res and isinstance(cuda_res, dict):
            if cuda_res.get("status") == "skipped":
                log("[hallucination] cuda stage skipped -> capability inconclusive")
            else:
                if isinstance(cuda_res.get("observed"), dict):
                    cuda_available_obs = bool(cuda_res["observed"].get("cuda_available"))
                    gpu_count_obs = int(cuda_res["observed"].get("gpu_count") or 0)
                else:
                    # Fallback: stage semantics for this benchmark check
                    cuda_available_obs = bool(cuda_res.get("exit_code") == 0)
                    gpu_count_obs = 0
        else:
            log(f"[hallucination] WARN: cannot read cuda stage results: {cuda_err}")

        observed["cuda_available"] = cuda_available_obs
        observed["gpu_count"] = gpu_count_obs

        reported_cuda = reported.get("cuda_available")
        if reported_cuda is True and cuda_available_obs is False:
            capability_items.append(
                {
                    "type": "cuda_available_overclaim",
                    "reported": True,
                    "observed": False,
                    "evidence": str(cuda_res_path),
                }
            )

        reported_gpu_count = reported.get("gpu_count")
        if isinstance(reported_gpu_count, int) and gpu_count_obs is not None and reported_gpu_count != gpu_count_obs:
            capability_items.append(
                {
                    "type": "gpu_count_mismatch",
                    "reported": reported_gpu_count,
                    "observed": gpu_count_obs,
                    "evidence": str(cuda_res_path),
                }
            )

        ddp_expected_ok = reported.get("ddp_expected_ok")
        if ddp_expected_ok is True:
            if gpu_count_obs is not None and gpu_count_obs < 2:
                log("[hallucination] ddp_expected_ok=true but <2 GPUs observed -> inconclusive (no hallucination)")
            else:
                if multi_res is None:
                    log(f"[hallucination] multi_gpu results missing/invalid -> inconclusive ({multi_err})")
                elif multi_res.get("status") == "skipped":
                    log("[hallucination] multi_gpu stage skipped -> ddp inconclusive (no hallucination)")
                else:
                    multi_failed = bool(multi_res.get("status") == "failure" or int(multi_res.get("exit_code", 1)) == 1)
                    if multi_failed:
                        capability_items.append(
                            {
                                "type": "ddp_expected_ok_but_multi_failed",
                                "reported": True,
                                "observed_multi_status": multi_res.get("status"),
                                "observed_multi_exit_code": multi_res.get("exit_code"),
                                "evidence": str(multi_res_path),
                            }
                        )
        elif ddp_expected_ok is False:
            if multi_res and isinstance(multi_res, dict) and multi_res.get("status") == "success":
                log("[hallucination] NOTE: ddp_expected_ok=false but multi_gpu succeeded (underclaim)")

        log("[hallucination] done")

    hallucinations = {
        "path": {"count": len(path_items), "items": path_items},
        "version": {"count": len(version_items), "items": version_items},
        "capability": {"count": len(capability_items), "items": capability_items},
    }

    any_hallucination = any(hallucinations[k]["count"] > 0 for k in hallucinations)
    report_missing_or_invalid = failure_category in ("missing_report", "invalid_json")

    status = "failure" if (any_hallucination or report_missing_or_invalid) else "success"
    exit_code = 1 if status == "failure" else 0

    if report_missing_or_invalid and not any_hallucination:
        failure_category = failure_category
    elif hallucinations["path"]["count"] > 0:
        failure_category = "path_hallucination"
    elif hallucinations["version"]["count"] > 0:
        failure_category = "version_hallucination"
    elif hallucinations["capability"]["count"] > 0:
        failure_category = "capability_hallucination"
    else:
        failure_category = "unknown"

    results = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"python {Path(__file__).name} --report-path {report_path}",
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
        "failure_category": failure_category,
        "error_excerpt": "",
    }
    write_json(results_path, results)

    # Fill error excerpt after log is complete.
    try:
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["error_excerpt"] = tail_lines(log_path)
        write_json(results_path, results)
    except Exception:
        pass

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

