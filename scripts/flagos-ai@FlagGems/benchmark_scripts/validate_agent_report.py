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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git_commit(repo_root: Path) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _default_report_path(cli_value: Optional[str]) -> str:
    if cli_value:
        return cli_value
    return os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"


def _read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, str(e)


def _run_python(python_path: str, code: str, timeout: int = 30) -> Tuple[int, str]:
    p = subprocess.run(
        [python_path, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=timeout,
    )
    return p.returncode, p.stdout


def _stage_results(repo_root: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], str]:
    p = repo_root / "build_output" / stage / "results.json"
    if not p.exists():
        return None, "missing"
    data, err = _read_json(p)
    if data is None:
        return None, f"invalid_json:{err}"
    return data, "ok"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "hallucination"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    report_path = _default_report_path(args.report_path)
    log(f"[hallucination] report_path={report_path}")
    report_file = Path(report_path)

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
        "stages": {},
    }

    reported: Dict[str, Any] = {}

    def add(kind: str, item: Dict[str, Any]) -> None:
        hallucinations[kind]["items"].append(item)
        hallucinations[kind]["count"] = len(hallucinations[kind]["items"])

    report_data: Optional[Dict[str, Any]]
    report_data, report_err = _read_json(report_file) if report_file.exists() else (None, "missing")
    if report_data is None:
        log(f"[hallucination] report read failed: {report_err}")
        add("path", {"type": "missing_report", "detail": f"{report_path}: {report_err}"})
        failure_category = "missing_report" if report_err == "missing" else "invalid_json"
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "hallucination",
            "report_path": report_path,
            "reported": reported,
            "observed": observed,
            "hallucinations": hallucinations,
            "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_timestamp()},
            "failure_category": failure_category,
            "error_excerpt": f"{report_path}: {report_err}",
        }
        results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    reported = report_data
    python_path = str(report_data.get("python_path", "") or "")
    observed["python_executable"] = python_path
    log(f"[hallucination] python_path={python_path}")

    # Path hallucination checks.
    if not python_path:
        add("path", {"type": "python_path_missing", "detail": "report.json missing python_path"})
    elif not (Path(python_path).exists() and os.access(python_path, os.X_OK)):
        add("path", {"type": "python_path_invalid", "detail": f"python_path not executable: {python_path}"})
    else:
        rc, out = _run_python(python_path, "import platform; print(platform.python_version())")
        if rc != 0:
            add("path", {"type": "python_path_unusable", "detail": out.strip()})
        else:
            observed["python_path_ok"] = True
            observed["python_version"] = out.strip().splitlines()[-1].strip()
            log(f"[hallucination] observed python_version={observed['python_version']}")

    # Version hallucinations.
    reported_pyver = report_data.get("python_version")
    if reported_pyver and observed.get("python_version") and str(reported_pyver) != str(observed["python_version"]):
        add(
            "version",
            {
                "type": "python_version_mismatch",
                "reported": str(reported_pyver),
                "observed": str(observed["python_version"]),
            },
        )

    if observed["python_path_ok"]:
        rc, out = _run_python(python_path, "import torch; print(torch.__version__)")
        if rc != 0:
            add("version", {"type": "torch_import_failed", "detail": out.strip()})
        else:
            observed["torch_import_ok"] = True
            observed["torch_version"] = out.strip().splitlines()[-1].strip()
            log(f"[hallucination] observed torch_version={observed['torch_version']}")
            reported_torchver = report_data.get("torch_version")
            if reported_torchver and str(reported_torchver) != str(observed["torch_version"]):
                add(
                    "version",
                    {
                        "type": "torch_version_mismatch",
                        "reported": str(reported_torchver),
                        "observed": str(observed["torch_version"]),
                    },
                )

    # Observed stage evidence.
    cuda_res, cuda_state = _stage_results(repo_root, "cuda")
    single_res, single_state = _stage_results(repo_root, "single_gpu")
    multi_res, multi_state = _stage_results(repo_root, "multi_gpu")
    cpu_res, cpu_state = _stage_results(repo_root, "cpu")
    log(f"[hallucination] stages: cuda={cuda_state} single={single_state} multi={multi_state} cpu={cpu_state}")

    observed["stages"] = {
        "cuda": {"state": cuda_state, "status": (cuda_res or {}).get("status"), "exit_code": (cuda_res or {}).get("exit_code")},
        "cpu": {"state": cpu_state, "status": (cpu_res or {}).get("status"), "exit_code": (cpu_res or {}).get("exit_code")},
        "single_gpu": {"state": single_state, "status": (single_res or {}).get("status"), "exit_code": (single_res or {}).get("exit_code")},
        "multi_gpu": {"state": multi_state, "status": (multi_res or {}).get("status"), "exit_code": (multi_res or {}).get("exit_code")},
    }

    if cuda_res and isinstance(cuda_res, dict):
        observed["cuda_available"] = bool(cuda_res.get("status") == "success" and int(cuda_res.get("exit_code", 1)) == 0)
        obs = cuda_res.get("observed") or {}
        if isinstance(obs, dict) and "gpu_count" in obs:
            observed["gpu_count"] = obs.get("gpu_count")

    if single_res and isinstance(single_res, dict):
        observed["single_gpu_exit_code"] = single_res.get("exit_code")

    if multi_res and isinstance(multi_res, dict):
        observed["multi_gpu_exit_code"] = multi_res.get("exit_code")

    # If gpu_count not available via cuda stage, measure via torch if possible.
    if observed["gpu_count"] is None and observed["python_path_ok"] and observed["torch_import_ok"]:
        rc, out = _run_python(python_path, "import torch; print(torch.cuda.device_count())")
        if rc == 0:
            try:
                observed["gpu_count"] = int(out.strip().splitlines()[-1].strip())
            except Exception:
                pass

    # Capability hallucinations (only where observed evidence is available and stage not skipped).
    reported_cuda = report_data.get("cuda_available")
    if reported_cuda is True and observed["cuda_available"] is False:
        add(
            "capability",
            {
                "type": "cuda_available_overclaim",
                "reported": True,
                "observed": False,
                "evidence": "build_output/cuda/results.json",
            },
        )

    reported_gpu_count = report_data.get("gpu_count")
    if isinstance(reported_gpu_count, int) and isinstance(observed.get("gpu_count"), int):
        if int(reported_gpu_count) != int(observed["gpu_count"]):
            add(
                "capability",
                {
                    "type": "gpu_count_mismatch",
                    "reported": int(reported_gpu_count),
                    "observed": int(observed["gpu_count"]),
                    "evidence": "cuda stage / torch.cuda.device_count()",
                },
            )

    ddp_expected_ok = report_data.get("ddp_expected_ok")
    if ddp_expected_ok is True:
        if isinstance(observed.get("gpu_count"), int) and observed["gpu_count"] >= 2:
            if multi_res and isinstance(multi_res, dict):
                if multi_res.get("status") != "skipped" and int(multi_res.get("exit_code", 1)) != 0:
                    add(
                        "capability",
                        {
                            "type": "ddp_expected_ok_but_multi_gpu_failed",
                            "reported": True,
                            "observed_multi_gpu_exit_code": int(multi_res.get("exit_code", 1)),
                            "evidence": "build_output/multi_gpu/results.json",
                        },
                    )
        else:
            # Inconclusive (<2 GPUs)
            pass

    # Determine final status.
    any_hallucination = (
        hallucinations["path"]["count"]
        + hallucinations["version"]["count"]
        + hallucinations["capability"]["count"]
    ) > 0

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

    payload = {
        "status": status,
        "exit_code": exit_code,
        "stage": "hallucination",
        "report_path": report_path,
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_timestamp()},
        "failure_category": failure_category,
        "error_excerpt": "",
    }

    results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
