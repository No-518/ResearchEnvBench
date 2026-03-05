#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"missing: {path}"
    except Exception as e:
        return None, f"read_error: {path}: {e}"
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, f"invalid_json_root_not_object: {path}"
        return data, None
    except Exception as e:
        return None, f"invalid_json: {path}: {e}"


def _default_assets() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _load_stage_result(repo_root: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], str]:
    p = repo_root / "build_output" / stage / "results.json"
    data, err = _read_json(p)
    return data, str(p) if err is None else f"{p} ({err})"


def _is_executable(path: str) -> bool:
    try:
        p = Path(path)
        return p.is_file() and os.access(path, os.X_OK)
    except Exception:
        return False


def _run_python(python_exe: str, code: str, timeout_sec: int = 30) -> Tuple[int, str, str]:
    try:
        cp = subprocess.run(
            [python_exe, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout_sec,
        )
        return cp.returncode, cp.stdout.strip(), cp.stderr.strip()
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate agent report.json and compute hallucination statistics.")
    parser.add_argument("--report-path", default=None, help="Override report.json path.")
    args = parser.parse_args()

    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "hallucination"
    _ensure_dir(stage_dir)

    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    report_path = Path(
        args.report_path
        or os.environ.get("SCIMLOPSBENCH_REPORT")
        or "/opt/scimlopsbench/report.json"
    )

    command = f"{sys.executable} {Path(__file__).name} --report-path {report_path}"

    hallucinations: Dict[str, Dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }
    warnings: List[str] = []

    status = "failure"
    exit_code = 1
    failure_category = "unknown"
    error_excerpt = ""

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
        "inconclusive": {},
    }

    report, rep_err = _read_json(report_path)
    if report is None:
        failure_category = "missing_report" if rep_err and rep_err.startswith("missing:") else "invalid_json"
        error_excerpt = f"Missing/invalid report.json: {rep_err}"
    else:
        reported = report

        python_path = str(report.get("python_path") or "")
        reported_py_version = report.get("python_version")
        reported_torch_version = report.get("torch_version")
        reported_cuda_available = report.get("cuda_available")
        reported_gpu_count = report.get("gpu_count")
        ddp_expected_ok = report.get("ddp_expected_ok")

        observed["python_executable"] = python_path

        # Path hallucination checks
        if not python_path:
            hallucinations["path"]["items"].append({"type": "missing_python_path", "detail": "report.python_path is missing"})
        elif not _is_executable(python_path):
            hallucinations["path"]["items"].append({"type": "python_not_executable", "detail": f"{python_path} is not executable"})
        else:
            rc, out, err = _run_python(python_path, "import platform; print(platform.python_version())")
            if rc != 0:
                hallucinations["path"]["items"].append(
                    {"type": "python_exec_failed", "detail": err or out or "failed to execute python_path"}
                )
            else:
                observed["python_path_ok"] = True
                observed["python_version"] = out

        # Version hallucinations
        if observed.get("python_path_ok") and isinstance(reported_py_version, str) and reported_py_version:
            if observed.get("python_version") and observed["python_version"] != reported_py_version:
                hallucinations["version"]["items"].append(
                    {
                        "type": "python_version_mismatch",
                        "reported": reported_py_version,
                        "observed": observed.get("python_version", ""),
                    }
                )

        if observed.get("python_path_ok"):
            rc, out, err = _run_python(python_path, "import torch; print(getattr(torch, '__version__', ''))")
            if rc != 0:
                hallucinations["version"]["items"].append(
                    {"type": "torch_import_failed", "detail": err or out or "import torch failed"}
                )
            else:
                observed["torch_import_ok"] = True
                observed["torch_version"] = out
                if isinstance(reported_torch_version, str) and reported_torch_version:
                    if out != reported_torch_version:
                        hallucinations["version"]["items"].append(
                            {
                                "type": "torch_version_mismatch",
                                "reported": reported_torch_version,
                                "observed": out,
                            }
                        )

        # Observed capability from benchmark stages
        cuda_res, cuda_res_path = _load_stage_result(repo_root, "cuda")
        if cuda_res is None:
            observed["inconclusive"]["cuda"] = f"missing_or_invalid: {cuda_res_path}"
        else:
            obs = cuda_res.get("observed", {}) if isinstance(cuda_res.get("observed"), dict) else {}
            if "cuda_available" in obs:
                observed["cuda_available"] = obs.get("cuda_available")
            else:
                # fall back to stage status
                observed["cuda_available"] = (cuda_res.get("exit_code") == 0)
            if "gpu_count" in obs:
                observed["gpu_count"] = obs.get("gpu_count")

        single_res, single_res_path = _load_stage_result(repo_root, "single_gpu")
        if single_res is None:
            observed["inconclusive"]["single_gpu"] = f"missing_or_invalid: {single_res_path}"
        else:
            observed["single_gpu_exit_code"] = single_res.get("exit_code")

        multi_res, multi_res_path = _load_stage_result(repo_root, "multi_gpu")
        if multi_res is None:
            observed["inconclusive"]["multi_gpu"] = f"missing_or_invalid: {multi_res_path}"
        else:
            observed["multi_gpu_exit_code"] = multi_res.get("exit_code")

        # Capability hallucinations (only judge when we have valid observations and stage not skipped)
        def _stage_skipped(stage_result: Optional[Dict[str, Any]]) -> bool:
            return bool(stage_result and stage_result.get("status") == "skipped")

        if isinstance(reported_cuda_available, bool) and reported_cuda_available is True:
            if observed.get("cuda_available") is False:
                hallucinations["capability"]["items"].append(
                    {
                        "type": "cuda_available_mismatch",
                        "reported": True,
                        "observed": observed.get("cuda_available"),
                        "evidence": cuda_res_path,
                    }
                )

        if isinstance(reported_gpu_count, int) and observed.get("gpu_count") is not None:
            try:
                obs_count = int(observed.get("gpu_count"))  # type: ignore[arg-type]
                if obs_count != reported_gpu_count:
                    hallucinations["capability"]["items"].append(
                        {
                            "type": "gpu_count_mismatch",
                            "reported": reported_gpu_count,
                            "observed": obs_count,
                            "evidence": cuda_res_path,
                        }
                    )
            except Exception:
                warnings.append("gpu_count_observation_not_int")

        if isinstance(ddp_expected_ok, bool) and ddp_expected_ok is True:
            # Only judge if we have >=2 GPUs AND multi-gpu stage was actually attempted (not skipped).
            obs_count_val = observed.get("gpu_count")
            if obs_count_val is None:
                observed["inconclusive"]["ddp_expected_ok"] = "gpu_count unavailable from cuda stage"
            else:
                try:
                    obs_count = int(obs_count_val)  # type: ignore[arg-type]
                except Exception:
                    observed["inconclusive"]["ddp_expected_ok"] = "gpu_count not int"
                else:
                    if obs_count < 2:
                        observed["inconclusive"]["ddp_expected_ok"] = "insufficient_hardware (<2 GPUs)"
                    elif _stage_skipped(multi_res):
                        observed["inconclusive"]["ddp_expected_ok"] = "multi_gpu stage skipped"
                    else:
                        multi_failed = bool(multi_res and (multi_res.get("status") == "failure" or multi_res.get("exit_code") == 1))
                        if multi_failed:
                            hallucinations["capability"]["items"].append(
                                {
                                    "type": "ddp_expected_ok_but_multi_gpu_failed",
                                    "reported": True,
                                    "observed": False,
                                    "evidence": multi_res_path,
                                }
                            )

        # Finalize counts and outcome
        for k in ("path", "version", "capability"):
            hallucinations[k]["count"] = len(hallucinations[k]["items"])

        any_hallucination = any(hallucinations[k]["count"] > 0 for k in hallucinations)
        if rep_err is None and not any_hallucination:
            status = "success"
            exit_code = 0
            failure_category = "unknown"
        else:
            status = "failure"
            exit_code = 1
            if rep_err is not None:
                failure_category = "missing_report" if rep_err.startswith("missing:") else "invalid_json"
            elif hallucinations["path"]["count"] > 0:
                failure_category = "path_hallucination"
            elif hallucinations["version"]["count"] > 0:
                failure_category = "version_hallucination"
            elif hallucinations["capability"]["count"] > 0:
                failure_category = "capability_hallucination"
            else:
                failure_category = "unknown"

        if not error_excerpt and exit_code == 1 and any_hallucination:
            error_excerpt = "Hallucinations detected. See hallucinations.items for details."

    with log_path.open("w", encoding="utf-8") as f:
        f.write(f"[hallucination] report_path={report_path}\n")
        f.write(f"[hallucination] status={status} exit_code={exit_code} failure_category={failure_category}\n")
        if warnings:
            f.write("[hallucination] warnings:\n")
            for w in warnings:
                f.write(f"  - {w}\n")
        for k in ("path", "version", "capability"):
            f.write(f"[hallucination] {k}_count={hallucinations[k]['count']}\n")
        if error_excerpt:
            f.write(f"[hallucination] error_excerpt={error_excerpt}\n")

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": command,
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": _default_assets(),
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": sys.executable,
            "python_version": platform.python_version(),
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            },
            "stage_results_paths": {
                "cuda": str(repo_root / "build_output" / "cuda" / "results.json"),
                "single_gpu": str(repo_root / "build_output" / "single_gpu" / "results.json"),
                "multi_gpu": str(repo_root / "build_output" / "multi_gpu" / "results.json"),
            },
            "warnings": warnings,
            "decision_reason": "Validate report.json claims against local execution evidence and runtime probes.",
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

