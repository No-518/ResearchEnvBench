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


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _default_assets() -> Dict[str, Any]:
    return {
        "dataset": {"path": "unknown", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        "model": {"path": "unknown", "source": "unknown", "version": "unknown", "sha256": "unknown"},
    }


def _load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception as e:
        return None, f"read_error: {e}"
    try:
        parsed = json.loads(raw)
    except Exception:
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "invalid_json"
    return parsed, None


def _load_stage_results(stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = REPO_ROOT / "build_output" / stage / "results.json"
    data, err = _load_json(path)
    if err:
        return None, err
    return data, None


def _tail_log(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    if len(lines) <= max_lines:
        return "\n".join(lines).strip()
    return "\n".join(lines[-max_lines:]).strip()


def _run_python(python_exe: str, code: str, timeout_sec: int = 30) -> Tuple[bool, str, str]:
    try:
        r = subprocess.run(
            [python_exe, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except Exception as e:
        return False, "", str(e)
    ok = r.returncode == 0
    return ok, (r.stdout or "").strip(), (r.stderr or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--timeout-sec", type=int, default=120)
    args = parser.parse_args()

    out_dir = REPO_ROOT / "build_output" / "hallucination"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(msg.rstrip() + "\n")

    report_path = Path(args.report_path) if args.report_path else Path(os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH))
    report, report_err = _load_json(report_path)
    if report_err or not report:
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "hallucination",
            "task": "validate",
            "command": f"{sys.executable} benchmark_scripts/validate_agent_report.py --report-path {report_path}",
            "timeout_sec": args.timeout_sec,
            "framework": "unknown",
            "assets": _default_assets(),
            "report_path": str(report_path),
            "reported": {},
            "observed": {},
            "hallucinations": {
                "path": {"count": 0, "items": []},
                "version": {"count": 0, "items": []},
                "capability": {"count": 0, "items": []},
            },
            "meta": {
                "python": sys.executable,
                "git_commit": "unknown",
                "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
                "decision_reason": "Validates report.json against real execution results and runtime checks.",
                "timestamp_utc": _utc_timestamp(),
            },
            "failure_category": report_err or "missing_report",
            "error_excerpt": "missing or invalid report.json",
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(payload["error_excerpt"])
        return 1

    reported_python_path = report.get("python_path")
    reported_python_version = report.get("python_version")
    reported_torch_version = report.get("torch_version")
    reported_cuda_available = report.get("cuda_available")
    reported_gpu_count = report.get("gpu_count")
    reported_ddp_expected_ok = report.get("ddp_expected_ok")

    reported = dict(report)

    path_items: List[Dict[str, Any]] = []
    version_items: List[Dict[str, Any]] = []
    capability_items: List[Dict[str, Any]] = []

    python_path_ok = False
    observed_python_version = None
    python_executable = str(reported_python_path) if isinstance(reported_python_path, str) else ""

    if not isinstance(reported_python_path, str) or not reported_python_path:
        path_items.append({"type": "missing_python_path", "detail": "python_path is missing from report.json"})
    else:
        p = Path(reported_python_path)
        if not (p.is_file() and os.access(str(p), os.X_OK)):
            path_items.append({"type": "python_path_not_executable", "detail": reported_python_path})
        else:
            ok, out, err = _run_python(reported_python_path, "import platform; print(platform.python_version())", timeout_sec=30)
            if not ok:
                path_items.append({"type": "python_invocation_failed", "detail": err or "unknown"})
            else:
                python_path_ok = True
                observed_python_version = out.strip()

    # Version checks
    torch_import_ok = False
    observed_torch_version = None
    if python_path_ok:
        ok, out, err = _run_python(
            reported_python_path,
            "import torch; print(torch.__version__)",
            timeout_sec=60,
        )
        if not ok:
            version_items.append({"type": "torch_import_failed", "detail": err or "import torch failed"})
        else:
            torch_import_ok = True
            observed_torch_version = out.strip()

    if isinstance(reported_python_version, str) and observed_python_version and reported_python_version != observed_python_version:
        version_items.append(
            {
                "type": "python_version_mismatch",
                "reported": reported_python_version,
                "observed": observed_python_version,
            }
        )

    if isinstance(reported_torch_version, str):
        if not torch_import_ok:
            version_items.append({"type": "torch_version_missing_observed", "reported": reported_torch_version})
        elif observed_torch_version and reported_torch_version != observed_torch_version:
            version_items.append(
                {
                    "type": "torch_version_mismatch",
                    "reported": reported_torch_version,
                    "observed": observed_torch_version,
                }
            )

    # Observed execution evidence (from build_output/*/results.json).
    cuda_results, cuda_err = _load_stage_results("cuda")
    single_results, single_err = _load_stage_results("single_gpu")
    multi_results, multi_err = _load_stage_results("multi_gpu")

    observed_cuda_available: Optional[bool] = None
    observed_gpu_count: Optional[int] = None
    if cuda_results and isinstance(cuda_results.get("observed"), dict):
        obs = cuda_results["observed"]
        if "cuda_available" in obs:
            observed_cuda_available = bool(obs.get("cuda_available"))
        if "gpu_count" in obs:
            try:
                observed_gpu_count = int(obs.get("gpu_count"))
            except Exception:
                observed_gpu_count = None
    elif cuda_results:
        # fallback: interpret stage exit code
        observed_cuda_available = (cuda_results.get("exit_code") == 0)

    single_exit_code = int(single_results.get("exit_code", 1)) if single_results else None
    multi_exit_code = int(multi_results.get("exit_code", 1)) if multi_results else None

    # Capability hallucinations (only when evidence is usable).
    cuda_stage_skipped = bool(cuda_results and cuda_results.get("status") == "skipped")
    single_stage_skipped = bool(single_results and single_results.get("status") == "skipped")
    multi_stage_skipped = bool(multi_results and multi_results.get("status") == "skipped")

    if isinstance(reported_cuda_available, bool) and not cuda_stage_skipped and observed_cuda_available is not None:
        if reported_cuda_available and not observed_cuda_available:
            capability_items.append(
                {
                    "type": "cuda_available_overclaim",
                    "reported": True,
                    "observed": False,
                    "evidence": "build_output/cuda/results.json",
                }
            )

    if isinstance(reported_gpu_count, int) and not cuda_stage_skipped and observed_gpu_count is not None:
        if reported_gpu_count != observed_gpu_count:
            capability_items.append(
                {
                    "type": "gpu_count_mismatch",
                    "reported": reported_gpu_count,
                    "observed": observed_gpu_count,
                    "evidence": "build_output/cuda/results.json",
                }
            )

    if isinstance(reported_ddp_expected_ok, bool) and reported_ddp_expected_ok:
        if observed_gpu_count is not None and observed_gpu_count >= 2:
            if not multi_stage_skipped and multi_results and multi_results.get("status") == "failure":
                capability_items.append(
                    {
                        "type": "ddp_expected_ok_but_failed",
                        "reported": True,
                        "observed": "failure",
                        "evidence": "build_output/multi_gpu/results.json",
                    }
                )
        else:
            # inconclusive: <2 GPUs or unknown
            pass

    hallucinations = {
        "path": {"count": len(path_items), "items": path_items},
        "version": {"count": len(version_items), "items": version_items},
        "capability": {"count": len(capability_items), "items": capability_items},
    }

    overall_hallucinations = len(path_items) + len(version_items) + len(capability_items)
    status = "success" if overall_hallucinations == 0 else "failure"
    exit_code = 0 if overall_hallucinations == 0 else 1

    if report_err:
        failure_category = report_err
    elif path_items:
        failure_category = "path_hallucination"
    elif version_items:
        failure_category = "version_hallucination"
    elif capability_items:
        failure_category = "capability_hallucination"
    else:
        failure_category = "unknown"

    observed = {
        "python_path_ok": python_path_ok,
        "python_executable": python_executable,
        "python_version": observed_python_version,
        "torch_import_ok": torch_import_ok,
        "torch_version": observed_torch_version,
        "cuda_available": observed_cuda_available,
        "gpu_count": observed_gpu_count,
        "single_gpu_exit_code": single_exit_code,
        "multi_gpu_exit_code": multi_exit_code,
        "evidence_load": {
            "cuda": "ok" if cuda_results else cuda_err,
            "single_gpu": "ok" if single_results else single_err,
            "multi_gpu": "ok" if multi_results else multi_err,
        },
        "skipped": {
            "cuda": bool(cuda_stage_skipped),
            "single_gpu": bool(single_stage_skipped),
            "multi_gpu": bool(multi_stage_skipped),
        },
    }

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"{sys.executable} benchmark_scripts/validate_agent_report.py --report-path {report_path}",
        "timeout_sec": args.timeout_sec,
        "framework": "unknown",
        "assets": _default_assets(),
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": sys.executable,
            "git_commit": "unknown",
            "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
            "decision_reason": "Path/version checks are executed against python_path; capability checks use build_output/*/results.json and ignore skipped stages.",
            "timestamp_utc": _utc_timestamp(),
        },
        "failure_category": failure_category,
        "error_excerpt": "" if status == "success" else _tail_log(log_path),
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if status != "success":
        log(json.dumps(hallucinations, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

