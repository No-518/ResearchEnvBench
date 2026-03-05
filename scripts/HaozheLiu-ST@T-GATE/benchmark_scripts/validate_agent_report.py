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


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_report_path(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def read_stage_results(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def run_python_snippet(python_path: str, code: str, timeout_sec: int = 30) -> tuple[int, str]:
    p = subprocess.run(
        [python_path, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec,
    )
    return p.returncode, p.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)

    result: dict[str, Any] = {
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
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    def add_item(kind: str, item: dict[str, Any]) -> None:
        result["hallucinations"][kind]["items"].append(item)
        result["hallucinations"][kind]["count"] = len(result["hallucinations"][kind]["items"])

    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as lf:
            lf.write(f"[hallucination] report_path={report_path}\n")
            lf.write(f"[hallucination] timestamp_utc={utc_now_iso()}\n")

        if not report_path.is_file():
            result["failure_category"] = "missing_report"
            raise FileNotFoundError(str(report_path))

        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            result["failure_category"] = "invalid_json"
            raise ValueError("report_json_not_object")

        result["reported"] = report

        python_path = report.get("python_path")
        reported_py_ver = report.get("python_version")
        reported_torch_ver = report.get("torch_version")
        reported_cuda_avail = report.get("cuda_available")
        reported_gpu_count = report.get("gpu_count")
        reported_ddp_ok = report.get("ddp_expected_ok")

        python_path_ok = isinstance(python_path, str) and bool(python_path) and os.path.isfile(python_path) and os.access(python_path, os.X_OK)
        result["observed"]["python_path_ok"] = bool(python_path_ok)
        result["observed"]["python_executable"] = str(python_path or "")

        if not isinstance(python_path, str) or not python_path:
            add_item("path", {"field": "python_path", "issue": "missing", "message": "python_path missing in report"})
        elif not python_path_ok:
            add_item("path", {"field": "python_path", "issue": "not_executable", "message": f"python_path not executable: {python_path}"})

        # If python_path isn't usable, stop here (still counts as path hallucination).
        if not python_path_ok:
            result["failure_category"] = "path_hallucination"
            raise RuntimeError("python_path not usable")

        rc, actual_py_ver = run_python_snippet(
            python_path, 'import platform; print(platform.python_version())', timeout_sec=30
        )
        if rc != 0 or not actual_py_ver:
            add_item("path", {"field": "python_path", "issue": "run_failed", "message": "python_path failed to run version probe"})
            result["failure_category"] = "path_hallucination"
            raise RuntimeError("python_path version probe failed")

        result["observed"]["python_version"] = actual_py_ver
        if isinstance(reported_py_ver, str) and reported_py_ver and reported_py_ver != actual_py_ver:
            add_item(
                "version",
                {"field": "python_version", "reported": reported_py_ver, "observed": actual_py_ver},
            )

        torch_probe = r"""
import json
out = {"torch_import_ok": False, "torch_version": "", "cuda_available": False, "gpu_count": 0, "error": ""}
try:
    import torch
    out["torch_import_ok"] = True
    out["torch_version"] = getattr(torch, "__version__", "")
    out["cuda_available"] = bool(torch.cuda.is_available())
    out["gpu_count"] = int(torch.cuda.device_count() if torch.cuda.is_available() else 0)
except Exception as e:
    out["error"] = repr(e)
print(json.dumps(out))
"""
        rc, out = run_python_snippet(python_path, torch_probe, timeout_sec=60)
        torch_obs: dict[str, Any] = {}
        try:
            torch_obs = json.loads(out.splitlines()[-1])
        except Exception:
            torch_obs = {"torch_import_ok": False, "error": "parse_failed"}

        result["observed"]["torch_import_ok"] = bool(torch_obs.get("torch_import_ok", False))
        result["observed"]["torch_version"] = str(torch_obs.get("torch_version", ""))

        if not result["observed"]["torch_import_ok"]:
            add_item("version", {"field": "torch_version", "issue": "import_failed", "message": str(torch_obs.get("error", ""))})
        elif isinstance(reported_torch_ver, str) and reported_torch_ver and reported_torch_ver != result["observed"]["torch_version"]:
            add_item("version", {"field": "torch_version", "reported": reported_torch_ver, "observed": result["observed"]["torch_version"]})

        # Capability hallucination uses benchmark stage results only when they are valid and not skipped.
        cuda_res = read_stage_results(root / "build_output" / "cuda" / "results.json")
        single_res = read_stage_results(root / "build_output" / "single_gpu" / "results.json")
        multi_res = read_stage_results(root / "build_output" / "multi_gpu" / "results.json")
        cpu_res = read_stage_results(root / "build_output" / "cpu" / "results.json")

        def stage_status(stage_res: dict[str, Any] | None) -> tuple[str, int]:
            if not stage_res:
                return "missing", 1
            status = str(stage_res.get("status", "missing"))
            raw_exit = stage_res.get("exit_code", 1)
            if raw_exit is None:
                exit_code = 1
            else:
                try:
                    exit_code = int(raw_exit)
                except Exception:
                    exit_code = 1
            return status, exit_code

        cuda_status, cuda_exit = stage_status(cuda_res)
        single_status, single_exit = stage_status(single_res)
        multi_status, multi_exit = stage_status(multi_res)
        cpu_status, cpu_exit = stage_status(cpu_res)

        result["observed"]["single_gpu_exit_code"] = single_exit
        result["observed"]["multi_gpu_exit_code"] = multi_exit
        result["observed"]["cpu_exit_code"] = cpu_exit

        cuda_stage_observed: dict[str, Any] | None = None
        if cuda_res and isinstance(cuda_res.get("observed"), dict):
            cuda_stage_observed = cuda_res.get("observed")  # type: ignore[assignment]

        cuda_has_observed = bool(
            cuda_stage_observed
            and isinstance(cuda_stage_observed.get("cuda_available"), (bool, int))
            and "gpu_count" in cuda_stage_observed
        )

        # Prefer stage-derived observation for reporting; otherwise fall back to torch probe only for reporting.
        if cuda_has_observed:
            observed_cuda_available = bool(cuda_stage_observed.get("cuda_available", False))  # type: ignore[union-attr]
            try:
                observed_gpu_count = int(cuda_stage_observed.get("gpu_count", 0) or 0)  # type: ignore[union-attr]
            except Exception:
                observed_gpu_count = None
            result["observed"]["capability_observation_source"] = "build_output/cuda/results.json"
        else:
            observed_cuda_available = bool(torch_obs.get("cuda_available", False))
            observed_gpu_count = int(torch_obs.get("gpu_count", 0) or 0)
            result["observed"]["capability_observation_source"] = "torch_probe_fallback"
            result["observed"]["capability_inconclusive_reason"] = "cuda_stage_missing_or_invalid_observed"

        result["observed"]["cuda_available"] = observed_cuda_available
        result["observed"]["gpu_count"] = observed_gpu_count

        # Capability hallucination judgments must use benchmark stage evidence (not fallback probes).
        if cuda_has_observed and isinstance(reported_cuda_avail, bool):
            if reported_cuda_avail is True and observed_cuda_available is False:
                add_item(
                    "capability",
                    {
                        "field": "cuda_available",
                        "reported": True,
                        "observed": False,
                        "evidence": "build_output/cuda/results.json",
                    },
                )

        if cuda_has_observed and isinstance(reported_gpu_count, int) and observed_gpu_count is not None:
            if int(reported_gpu_count) != int(observed_gpu_count):
                add_item(
                    "capability",
                    {
                        "field": "gpu_count",
                        "reported": int(reported_gpu_count),
                        "observed": int(observed_gpu_count),
                        "evidence": "build_output/cuda/results.json",
                    },
                )

        # DDP capability hallucination (only when multi_gpu stage actually ran and GPUs>=2).
        if isinstance(reported_ddp_ok, bool) and reported_ddp_ok is True:
            if not cuda_has_observed:
                result["observed"]["ddp_inconclusive_reason"] = "gpu_count_unknown_no_cuda_stage_observed"
            elif multi_res and multi_status == "skipped":
                # Explicitly inconclusive; do not count as hallucination.
                result["observed"]["ddp_inconclusive_reason"] = "multi_gpu_stage_skipped"
            elif observed_gpu_count is not None and observed_gpu_count < 2:
                result["observed"]["ddp_inconclusive_reason"] = "gpu_count_lt_2"
            elif multi_res and multi_status in ("success", "failure") and multi_exit != 0:
                add_item("capability", {"field": "ddp_expected_ok", "reported": True, "observed": False, "evidence": "build_output/multi_gpu/results.json"})

        # Determine overall status and failure_category.
        any_path = result["hallucinations"]["path"]["count"] > 0
        any_ver = result["hallucinations"]["version"]["count"] > 0
        any_cap = result["hallucinations"]["capability"]["count"] > 0

        if any_path:
            result["failure_category"] = "path_hallucination"
        elif any_ver:
            result["failure_category"] = "version_hallucination"
        elif any_cap:
            result["failure_category"] = "capability_hallucination"
        else:
            result["failure_category"] = ""

        if any_path or any_ver or any_cap:
            result["status"] = "failure"
            result["exit_code"] = 1
        else:
            result["status"] = "success"
            result["exit_code"] = 0

        result["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if result["exit_code"] == 0 else 1
    except Exception as e:
        with log_path.open("a", encoding="utf-8", errors="replace") as lf:
            lf.write("\n[hallucination] exception:\n")
            lf.write(str(e) + "\n")
            lf.write(traceback.format_exc())
        if result["failure_category"] in ("missing_report", "invalid_json"):
            result["status"] = "failure"
            result["exit_code"] = 1
        result["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
