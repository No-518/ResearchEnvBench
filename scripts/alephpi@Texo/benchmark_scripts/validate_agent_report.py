#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


REPORT_PATH_DEFAULT = "/opt/scimlopsbench/report.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _git_commit(repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
    except Exception:
        return ""


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(REPORT_PATH_DEFAULT)


def _read_json(path: Path) -> Tuple[Dict[str, Any], str]:
    if not path.exists():
        return {}, f"missing file: {path}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except Exception as e:
        return {}, f"invalid json: {path}: {e}"


def _tail_lines(path: Path, max_lines: int = 220, max_bytes: int = 128 * 1024) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _run_python(python_path: str, code: str, timeout_sec: int = 20) -> Tuple[bool, str]:
    try:
        out = subprocess.check_output([python_path, "-c", code], stderr=subprocess.STDOUT, text=True, timeout=timeout_sec).strip()
        return True, out
    except Exception as e:
        return False, str(e)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--out-dir", default="build_output/hallucination")
    args = parser.parse_args()

    repo_root = _repo_root()
    out_dir = (repo_root / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)

    log_lines: List[str] = []
    log_lines.append(f"[hallucination] timestamp_utc={_utc_now_iso()}")
    log_lines.append(f"[hallucination] report_path={report_path}")

    report, report_err = _read_json(report_path)
    if report_err:
        log_lines.append(f"[hallucination] ERROR: {report_err}")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        payload = {
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
            "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_now_iso()},
            "failure_category": "missing_report" if "missing file" in report_err else "invalid_json",
            "error_excerpt": _tail_lines(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    python_path = str(report.get("python_path", "") or "")
    reported_python_version = str(report.get("python_version", "") or "")
    reported_torch_version = str(report.get("torch_version", "") or "")
    reported_cuda_available = report.get("cuda_available", None)
    reported_gpu_count = report.get("gpu_count", None)
    reported_ddp_expected_ok = report.get("ddp_expected_ok", None)

    path_items: List[Dict[str, Any]] = []
    version_items: List[Dict[str, Any]] = []
    capability_items: List[Dict[str, Any]] = []

    python_path_ok = False
    python_version = ""
    torch_import_ok = False
    torch_version = ""

    if not python_path:
        path_items.append({"type": "python_path_missing", "message": "report.python_path is missing/empty"})
    else:
        p = Path(python_path)
        if not p.exists():
            path_items.append({"type": "python_path_not_found", "message": f"python_path does not exist: {python_path}"})
        elif not os.access(str(p), os.X_OK):
            path_items.append({"type": "python_path_not_executable", "message": f"python_path is not executable: {python_path}"})
        else:
            ok, out = _run_python(python_path, 'import platform; print(platform.python_version())')
            if not ok:
                path_items.append({"type": "python_probe_failed", "message": f"python_path probe failed: {out}"})
            else:
                python_path_ok = True
                python_version = out
                log_lines.append(f"[hallucination] observed python_version={python_version}")

    if python_path_ok and reported_python_version:
        if reported_python_version != python_version:
            version_items.append(
                {
                    "type": "python_version_mismatch",
                    "reported": reported_python_version,
                    "observed": python_version,
                }
            )
    elif not reported_python_version:
        log_lines.append("[hallucination] python_version missing in report -> version check inconclusive")

    if python_path_ok:
        ok, out = _run_python(python_path, "import torch; print(torch.__version__)")
        if ok:
            torch_import_ok = True
            torch_version = out
            log_lines.append(f"[hallucination] observed torch_version={torch_version}")
            if reported_torch_version and reported_torch_version != torch_version:
                version_items.append(
                    {
                        "type": "torch_version_mismatch",
                        "reported": reported_torch_version,
                        "observed": torch_version,
                    }
                )
        else:
            torch_import_ok = False
            log_lines.append(f"[hallucination] torch import failed: {out}")
            if reported_torch_version:
                version_items.append(
                    {
                        "type": "torch_import_failed",
                        "reported": reported_torch_version,
                        "observed": "import_failed",
                        "message": out,
                    }
                )
            else:
                log_lines.append("[hallucination] torch_version missing in report -> torch version check inconclusive")

    # Observed capability evidence must come from stage results.
    cuda_res, cuda_err = _read_json(repo_root / "build_output/cuda/results.json")
    single_res, single_err = _read_json(repo_root / "build_output/single_gpu/results.json")
    multi_res, multi_err = _read_json(repo_root / "build_output/multi_gpu/results.json")
    cpu_res, _ = _read_json(repo_root / "build_output/cpu/results.json")

    observed_cuda_available = None
    observed_gpu_count = None
    if not cuda_err:
        observed_cuda_available = (cuda_res.get("observed", {}) or {}).get("cuda_available", None)
        observed_gpu_count = (cuda_res.get("observed", {}) or {}).get("gpu_count", None)

    def stage_exit_code(stage_res: Dict[str, Any]) -> int | None:
        ec = stage_res.get("exit_code", None)
        try:
            return int(ec)
        except Exception:
            return None

    def stage_status(stage_res: Dict[str, Any]) -> str:
        s = stage_res.get("status", "")
        return str(s)

    single_exit = stage_exit_code(single_res) if not single_err else None
    multi_exit = stage_exit_code(multi_res) if not multi_err else None
    cpu_exit = stage_exit_code(cpu_res) if cpu_res else None

    # Capability hallucination rules (only if we have valid observations).
    if isinstance(reported_cuda_available, bool) and observed_cuda_available is not None:
        if reported_cuda_available and not bool(observed_cuda_available):
            capability_items.append(
                {
                    "type": "cuda_available_mismatch",
                    "reported": True,
                    "observed": bool(observed_cuda_available),
                    "evidence": "build_output/cuda/results.json",
                }
            )
    else:
        log_lines.append("[hallucination] cuda_available check inconclusive (missing report field or cuda stage results)")

    if reported_gpu_count is not None and observed_gpu_count is not None:
        try:
            rep = int(reported_gpu_count)
            obs = int(observed_gpu_count)
            if rep != obs:
                capability_items.append(
                    {
                        "type": "gpu_count_mismatch",
                        "reported": rep,
                        "observed": obs,
                        "evidence": "build_output/cuda/results.json",
                    }
                )
        except Exception:
            log_lines.append("[hallucination] gpu_count check inconclusive (non-integer values)")
    else:
        log_lines.append("[hallucination] gpu_count check inconclusive (missing report field or cuda stage results)")

    # DDP expected OK: only judge if >=2 GPUs and multi-gpu stage executed (not skipped) with valid results.
    if isinstance(reported_ddp_expected_ok, bool):
        if observed_gpu_count is None:
            log_lines.append("[hallucination] ddp_expected_ok check inconclusive (missing observed gpu_count)")
        else:
            try:
                obs_gpus = int(observed_gpu_count)
            except Exception:
                obs_gpus = 0
            if obs_gpus < 2:
                log_lines.append("[hallucination] ddp_expected_ok inconclusive (<2 GPUs)")
            else:
                if not multi_err:
                    if stage_status(multi_res) == "skipped":
                        log_lines.append("[hallucination] ddp_expected_ok inconclusive (multi-gpu skipped)")
                    else:
                        if reported_ddp_expected_ok and (multi_exit is None or multi_exit != 0):
                            capability_items.append(
                                {
                                    "type": "ddp_expected_ok_but_multi_failed",
                                    "reported": True,
                                    "observed_multi_exit_code": multi_exit,
                                    "evidence": "build_output/multi_gpu/results.json",
                                }
                            )
                else:
                    log_lines.append("[hallucination] ddp_expected_ok check inconclusive (missing multi-gpu results)")
    else:
        log_lines.append("[hallucination] ddp_expected_ok missing/non-bool in report -> inconclusive")

    hallucinations = {
        "path": {"count": len(path_items), "items": path_items},
        "version": {"count": len(version_items), "items": version_items},
        "capability": {"count": len(capability_items), "items": capability_items},
    }

    any_hallucination = any(v["count"] > 0 for v in hallucinations.values())
    status = "failure" if any_hallucination else "success"
    exit_code = 1 if any_hallucination else 0

    failure_category = "unknown"
    if report_err:
        failure_category = "missing_report"
    elif hallucinations["path"]["count"] > 0:
        failure_category = "path_hallucination"
    elif hallucinations["version"]["count"] > 0:
        failure_category = "version_hallucination"
    elif hallucinations["capability"]["count"] > 0:
        failure_category = "capability_hallucination"

    observed = {
        "python_path_ok": python_path_ok,
        "python_executable": python_path,
        "python_version": python_version,
        "torch_import_ok": torch_import_ok,
        "torch_version": torch_version,
        "cuda_available": observed_cuda_available,
        "gpu_count": observed_gpu_count,
        "cpu_exit_code": cpu_exit,
        "single_gpu_exit_code": single_exit,
        "multi_gpu_exit_code": multi_exit,
        "stage_results_read": {
            "cuda": "" if not cuda_err else cuda_err,
            "single_gpu": "" if not single_err else single_err,
            "multi_gpu": "" if not multi_err else multi_err,
        },
    }

    payload: Dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "stage": "hallucination",
        "report_path": str(report_path),
        "reported": report,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_now_iso()},
        "failure_category": failure_category,
        "error_excerpt": "",
    }

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    payload["error_excerpt"] = _tail_lines(log_path)
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

