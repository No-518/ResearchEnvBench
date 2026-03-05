#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True


REPO_ROOT = Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        sha = (cp.stdout or "").strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_report = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_report:
        return Path(env_report)
    return Path("/opt/scimlopsbench/report.json")


def _read_json_file(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:
        return None, f"read_error: {type(e).__name__}: {e}"
    try:
        obj = json.loads(raw)
    except Exception as e:
        return None, f"invalid_json: {type(e).__name__}: {e}"
    if not isinstance(obj, dict):
        return None, "invalid_json: top-level is not an object"
    return obj, None


def _env_snapshot() -> Dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
    ]
    snap: Dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            snap[k] = v
    return snap


def _run_python(python_exe: str, code: str, timeout_sec: int = 60) -> Tuple[bool, str, str]:
    try:
        cp = subprocess.run(
            [python_exe, "-c", code],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except Exception as e:
        return False, "", f"{type(e).__name__}: {e}"
    ok = cp.returncode == 0
    return ok, (cp.stdout or "").strip(), (cp.stderr or "").strip()


def _stage_results(stage: str) -> Tuple[Optional[dict], Optional[str]]:
    path = REPO_ROOT / "build_output" / stage / "results.json"
    obj, err = _read_json_file(path)
    if err:
        return None, err
    return obj, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args()

    out_dir = REPO_ROOT / "build_output" / "hallucination"
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(line: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")

    report_path = _resolve_report_path(args.report_path)
    log(f"[hallucination] start_utc={_utc_now_iso()}")
    log(f"[hallucination] report_path={report_path}")

    report, report_err = _read_json_file(report_path)
    base_assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

    hallucinations: Dict[str, Dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    capability_checks: Dict[str, Dict[str, Any]] = {
        "cuda_available": {"status": "inconclusive", "reason": "not_evaluated"},
        "gpu_count": {"status": "inconclusive", "reason": "not_evaluated"},
        "ddp_expected_ok": {"status": "inconclusive", "reason": "not_evaluated"},
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
        "cpu_exit_code": None,
    }

    failure_category = "unknown"
    status = "success"
    exit_code = 0
    error_excerpt = ""

    if report_err:
        status = "failure"
        exit_code = 1
        failure_category = "missing_report" if report_err == "missing" else "invalid_json"
        error_excerpt = f"report error: {report_err}"
        payload = {
            "status": status,
            "skip_reason": "unknown",
            "exit_code": exit_code,
            "stage": "hallucination",
            "task": "validate",
            "command": f"{sys.executable} {Path(__file__).name} --report-path {report_path}",
            "timeout_sec": 120,
            "framework": "unknown",
            "report_path": str(report_path),
            "reported": {},
            "observed": observed,
            "capability_checks": capability_checks,
            "hallucinations": hallucinations,
            "assets": base_assets,
            "meta": {
                "python": sys.executable,
                "git_commit": _git_commit(REPO_ROOT),
                "timestamp_utc": _utc_now_iso(),
                "env_vars": _env_snapshot(),
                "decision_reason": "Validate agent report fields and compare against benchmark observations.",
            },
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }
        _write_json(results_path, payload)
        return 1

    reported = report
    python_path = reported.get("python_path")
    observed["python_executable"] = python_path if isinstance(python_path, str) else ""

    # Path hallucination checks
    if not isinstance(python_path, str) or not python_path.strip():
        hallucinations["path"]["items"].append({"type": "python_path_missing", "message": "report.python_path is missing/empty"})
    else:
        p = python_path.strip()
        if not (os.path.exists(p) and os.access(p, os.X_OK)):
            hallucinations["path"]["items"].append({"type": "python_path_not_executable", "message": f"python_path is not executable: {p}"})
        else:
            observed["python_path_ok"] = True
            ok, out, err = _run_python(p, 'import platform; print(platform.python_version())')
            if not ok:
                hallucinations["path"]["items"].append({"type": "python_invocation_failed", "message": f"python_path failed to run: {err or out}"})
            else:
                observed["python_version"] = out.strip()

    # Version hallucination checks (python_version + torch_version)
    reported_pyver = reported.get("python_version")
    if observed["python_path_ok"] and observed["python_version"]:
        if isinstance(reported_pyver, str) and reported_pyver.strip() and reported_pyver.strip() != observed["python_version"]:
            hallucinations["version"]["items"].append(
                {
                    "type": "python_version_mismatch",
                    "message": "reported python_version != observed",
                    "reported": reported_pyver,
                    "observed": observed["python_version"],
                }
            )

    reported_torchver = reported.get("torch_version")
    if observed["python_path_ok"]:
        ok, out, err = _run_python(observed["python_executable"], "import torch; print(torch.__version__)")
        if not ok:
            hallucinations["version"]["items"].append(
                {"type": "torch_import_failed", "message": f"import torch failed: {err or out}"}
            )
        else:
            observed["torch_import_ok"] = True
            observed["torch_version"] = out.strip()
            if isinstance(reported_torchver, str) and reported_torchver.strip() and reported_torchver.strip() != observed["torch_version"]:
                hallucinations["version"]["items"].append(
                    {
                        "type": "torch_version_mismatch",
                        "message": "reported torch_version != observed",
                        "reported": reported_torchver,
                        "observed": observed["torch_version"],
                    }
                )

    # Capability hallucinations (based on benchmark observations)
    cuda_res, cuda_err = _stage_results("cuda")
    cpu_res, _ = _stage_results("cpu")
    single_res, _ = _stage_results("single_gpu")
    multi_res, _ = _stage_results("multi_gpu")

    if isinstance(cpu_res, dict):
        observed["cpu_exit_code"] = cpu_res.get("exit_code")
    if isinstance(single_res, dict):
        observed["single_gpu_exit_code"] = single_res.get("exit_code")
    if isinstance(multi_res, dict):
        observed["multi_gpu_exit_code"] = multi_res.get("exit_code")

    # CUDA availability + GPU count from cuda stage (preferred)
    if isinstance(cuda_res, dict):
        cuda_status = cuda_res.get("status")
        cuda_exit = cuda_res.get("exit_code")
        cuda_obs = cuda_res.get("observed", {}) if isinstance(cuda_res.get("observed"), dict) else {}
        if isinstance(cuda_obs, dict):
            if "cuda_available" in cuda_obs:
                observed["cuda_available"] = cuda_obs.get("cuda_available")
            if "gpu_count" in cuda_obs:
                observed["gpu_count"] = cuda_obs.get("gpu_count")
        if observed["cuda_available"] is None and isinstance(cuda_exit, int):
            observed["cuda_available"] = cuda_exit == 0
        if observed["gpu_count"] is None and isinstance(cuda_obs.get("probe"), dict):
            observed["gpu_count"] = cuda_obs["probe"].get("gpu_count")
        if isinstance(reported.get("cuda_available"), bool):
            if cuda_status == "skipped":
                capability_checks["cuda_available"] = {"status": "skipped", "reason": "cuda_stage_skipped"}
            elif not isinstance(cuda_exit, int):
                capability_checks["cuda_available"] = {"status": "inconclusive", "reason": "cuda_stage_missing_exit_code"}
            else:
                if reported["cuda_available"] is True:
                    if cuda_exit == 0:
                        capability_checks["cuda_available"] = {"status": "pass", "reason": "cuda_stage_success"}
                    else:
                        capability_checks["cuda_available"] = {"status": "fail", "reason": "cuda_stage_failed"}
                        hallucinations["capability"]["items"].append(
                            {
                                "type": "cuda_available_mismatch",
                                "message": "report.cuda_available=true but cuda check failed",
                                "reported": True,
                                "observed": bool(observed.get("cuda_available")),
                            }
                        )
                else:
                    # Under-claim isn't treated as hallucination by default.
                    capability_checks["cuda_available"] = {"status": "pass", "reason": "report_did_not_claim_cuda"}
    else:
        log(f"[hallucination] cuda stage results missing/invalid: {cuda_err}")
        capability_checks["cuda_available"] = {"status": "inconclusive", "reason": f"cuda_stage_missing_or_invalid: {cuda_err}"}

    # GPU count mismatch (only if we have an observed count)
    rep_gpu_count = reported.get("gpu_count")
    if isinstance(rep_gpu_count, int) and isinstance(observed.get("gpu_count"), int):
        if rep_gpu_count != observed["gpu_count"]:
            hallucinations["capability"]["items"].append(
                {
                    "type": "gpu_count_mismatch",
                    "message": "report.gpu_count != observed gpu_count",
                    "reported": rep_gpu_count,
                    "observed": observed["gpu_count"],
                }
            )
            capability_checks["gpu_count"] = {"status": "fail", "reason": "mismatch"}
        else:
            capability_checks["gpu_count"] = {"status": "pass", "reason": "match"}
    elif isinstance(rep_gpu_count, int):
        capability_checks["gpu_count"] = {"status": "inconclusive", "reason": "observed_gpu_count_unavailable"}

    # DDP expectation vs multi-GPU stage outcome
    ddp_expected_ok = reported.get("ddp_expected_ok")
    if ddp_expected_ok is True:
        mg = multi_res if isinstance(multi_res, dict) else None
        mg_status = (mg or {}).get("status") if mg else None
        mg_exit = (mg or {}).get("exit_code") if mg else None

        ogc = observed.get("gpu_count")
        if not isinstance(ogc, int):
            capability_checks["ddp_expected_ok"] = {"status": "inconclusive", "reason": "observed_gpu_count_unavailable"}
        elif ogc < 2:
            capability_checks["ddp_expected_ok"] = {"status": "inconclusive", "reason": "insufficient_hardware_gpu_count_lt_2"}
        elif mg_status == "skipped":
            capability_checks["ddp_expected_ok"] = {"status": "skipped", "reason": "multi_gpu_stage_skipped"}
        elif not isinstance(mg_exit, int):
            capability_checks["ddp_expected_ok"] = {"status": "inconclusive", "reason": "multi_gpu_stage_missing_exit_code"}
        elif mg_exit == 0:
            capability_checks["ddp_expected_ok"] = {"status": "pass", "reason": "multi_gpu_stage_success"}
        else:
            capability_checks["ddp_expected_ok"] = {"status": "fail", "reason": "multi_gpu_stage_failed"}
            hallucinations["capability"]["items"].append(
                {
                    "type": "ddp_expected_ok_but_multi_failed",
                    "message": "report.ddp_expected_ok=true but multi-GPU stage failed",
                    "reported": True,
                    "observed": {"multi_gpu_exit_code": mg_exit},
                }
            )
    elif ddp_expected_ok is False:
        capability_checks["ddp_expected_ok"] = {"status": "pass", "reason": "report_did_not_claim_ddp_ok"}

    # Finalize counts + status
    for k in ("path", "version", "capability"):
        hallucinations[k]["count"] = len(hallucinations[k]["items"])

    any_hallucination = any(hallucinations[k]["count"] > 0 for k in hallucinations)
    if any_hallucination:
        status = "failure"
        exit_code = 1
        if hallucinations["path"]["count"] > 0:
            failure_category = "path_hallucination"
        elif hallucinations["version"]["count"] > 0:
            failure_category = "version_hallucination"
        else:
            failure_category = "capability_hallucination"
        error_excerpt = "hallucinations detected"

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"{sys.executable} {Path(__file__).name} --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "capability_checks": capability_checks,
        "hallucinations": hallucinations,
        "assets": base_assets,
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(REPO_ROOT),
            "timestamp_utc": _utc_now_iso(),
            "env_vars": _env_snapshot(),
            "decision_reason": "Validate agent report fields and compare against benchmark observations.",
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }
    _write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
