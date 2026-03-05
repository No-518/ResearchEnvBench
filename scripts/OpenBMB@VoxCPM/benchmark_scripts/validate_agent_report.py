#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def get_git_commit(root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def tail_lines(path: Path, n: int = 240) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def load_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"Missing JSON file: {path}"
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON in {path}: {e}"
    except Exception as e:
        return None, f"Failed to read {path}: {e}"


def resolve_report_path(cli: Optional[str]) -> Path:
    if cli:
        p = Path(cli)
        return (p / "report.json") if p.is_dir() else p
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        p = Path(os.environ["SCIMLOPSBENCH_REPORT"])
        return (p / "report.json") if p.is_dir() else p
    p = Path("/opt/scimlopsbench/report.json")
    return (p / "report.json") if p.is_dir() else p


def python_exec_ok(p: str) -> bool:
    pp = Path(p)
    return pp.exists() and pp.is_file() and os.access(str(pp), os.X_OK)


def run_python(python: str, code: str, timeout_sec: int = 30) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(
            [python, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return proc.returncode == 0, (proc.stdout or "").strip()
    except Exception as e:
        return False, str(e)


def stage_result_path(stage: str) -> Path:
    return repo_root() / "build_output" / stage / "results.json"


def read_stage_observation(stage: str) -> Tuple[Optional[dict], Optional[str]]:
    p = stage_result_path(stage)
    d, err = load_json(p)
    if d is None:
        return None, err
    if not isinstance(d, dict):
        return None, f"Invalid stage results (not an object): {p}"
    return d, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    report_path = resolve_report_path(args.report_path)

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")

    report, report_err = load_json(report_path)
    if report is None or not isinstance(report, dict):
        log(f"ERROR: {report_err or 'invalid report.json'}")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
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
            "reported": {},
            "observed": {},
            "hallucinations": {
                "path": {"count": 0, "items": []},
                "version": {"count": 0, "items": []},
                "capability": {"count": 0, "items": []},
            },
            "meta": {"git_commit": get_git_commit(root), "timestamp_utc": utc_now_iso()},
            "failure_category": "missing_report" if report_err and "Missing" in report_err else "invalid_json",
            "error_excerpt": tail_lines(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    reported = {
        "python_path": report.get("python_path"),
        "python_version": report.get("python_version"),
        "torch_version": report.get("torch_version"),
        "cuda_available": report.get("cuda_available"),
        "gpu_count": report.get("gpu_count"),
        "ddp_expected_ok": report.get("ddp_expected_ok"),
        "notes": report.get("notes", ""),
    }
    log(f"Loaded report: {reported}")

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
        "cpu_exit_code": None,
        "inconclusive": {},
    }

    # -----------------------------
    # Path hallucination checks
    # -----------------------------
    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path:
        hallucinations["path"]["items"].append({"type": "python_path_missing", "detail": "report.python_path is missing"})
    elif not python_exec_ok(python_path):
        hallucinations["path"]["items"].append(
            {"type": "python_path_not_executable", "detail": f"python_path not executable: {python_path}"}
        )
    else:
        ok, out = run_python(python_path, 'import platform; print(platform.python_version())', timeout_sec=30)
        if not ok:
            hallucinations["path"]["items"].append(
                {"type": "python_path_unusable", "detail": f"python_path failed to run: {out}"}
            )
        else:
            observed["python_path_ok"] = True
            observed["python_executable"] = python_path
            observed["python_version"] = out.strip()

    # -----------------------------
    # Version hallucination checks
    # -----------------------------
    if observed["python_path_ok"]:
        reported_pyver = report.get("python_version")
        if isinstance(reported_pyver, str) and reported_pyver and observed["python_version"]:
            if reported_pyver.strip() != observed["python_version"].strip():
                hallucinations["version"]["items"].append(
                    {
                        "type": "python_version_mismatch",
                        "reported": reported_pyver,
                        "observed": observed["python_version"],
                    }
                )

        ok, out = run_python(python_path, "import torch; print(torch.__version__)", timeout_sec=60)
        if not ok:
            hallucinations["version"]["items"].append({"type": "torch_import_failed", "detail": out})
        else:
            observed["torch_import_ok"] = True
            observed["torch_version"] = out.strip()
            reported_torch = report.get("torch_version")
            if isinstance(reported_torch, str) and reported_torch and observed["torch_version"]:
                if reported_torch.strip() != observed["torch_version"].strip():
                    hallucinations["version"]["items"].append(
                        {
                            "type": "torch_version_mismatch",
                            "reported": reported_torch,
                            "observed": observed["torch_version"],
                        }
                    )

    # -----------------------------
    # Capability hallucination checks (based on stage results)
    # -----------------------------
    cuda_res, cuda_err = read_stage_observation("cuda")
    if cuda_res is None:
        observed["inconclusive"]["cuda"] = cuda_err or "missing"
        log(f"[inconclusive] cuda results: {cuda_err}")
    else:
        cuda_obs = cuda_res.get("observed", {}) if isinstance(cuda_res, dict) else {}
        observed["cuda_available"] = cuda_obs.get("cuda_available")
        observed["gpu_count"] = cuda_obs.get("gpu_count")
        log(f"Observed from cuda stage: cuda_available={observed['cuda_available']} gpu_count={observed['gpu_count']}")

        if report.get("cuda_available") is True:
            if cuda_res.get("status") == "skipped":
                observed["inconclusive"]["cuda_available"] = "cuda stage skipped"
            elif int(cuda_res.get("exit_code", 1)) != 0:
                hallucinations["capability"]["items"].append(
                    {"type": "cuda_available_overclaim", "reported": True, "observed": False}
                )

        if isinstance(report.get("gpu_count"), int) and isinstance(observed["gpu_count"], int):
            if int(report["gpu_count"]) != int(observed["gpu_count"]):
                hallucinations["capability"]["items"].append(
                    {"type": "gpu_count_mismatch", "reported": int(report["gpu_count"]), "observed": int(observed["gpu_count"])}
                )

    # Single-GPU stage exit code (optional evidence)
    sg_res, sg_err = read_stage_observation("single_gpu")
    if sg_res is None:
        observed["inconclusive"]["single_gpu"] = sg_err or "missing"
        log(f"[inconclusive] single_gpu results: {sg_err}")
    else:
        observed["single_gpu_exit_code"] = int(sg_res.get("exit_code", 1))

    # Multi-GPU stage exit code (capability evidence)
    mg_res, mg_err = read_stage_observation("multi_gpu")
    if mg_res is None:
        observed["inconclusive"]["multi_gpu"] = mg_err or "missing"
        log(f"[inconclusive] multi_gpu results: {mg_err}")
    else:
        observed["multi_gpu_exit_code"] = int(mg_res.get("exit_code", 1))

    # CPU stage exit code (optional)
    cpu_res, cpu_err = read_stage_observation("cpu")
    if cpu_res is None:
        observed["inconclusive"]["cpu"] = cpu_err or "missing"
        log(f"[inconclusive] cpu results: {cpu_err}")
    else:
        if cpu_res.get("status") == "skipped":
            observed["inconclusive"]["cpu"] = f"cpu stage skipped ({cpu_res.get('skip_reason', 'not_applicable')})"
        else:
            observed["cpu_exit_code"] = int(cpu_res.get("exit_code", 1))

    # DDP capability: only judge if report claims ddp_expected_ok == True AND we have >=2 GPUs observed.
    if report.get("ddp_expected_ok") is True:
        if isinstance(observed.get("gpu_count"), int) and observed["gpu_count"] < 2:
            observed["inconclusive"]["ddp_expected_ok"] = "gpu_count < 2"
        elif mg_res is None:
            observed["inconclusive"]["ddp_expected_ok"] = "multi_gpu results missing"
        elif mg_res.get("status") == "skipped":
            observed["inconclusive"]["ddp_expected_ok"] = "multi_gpu stage skipped"
        else:
            if int(mg_res.get("exit_code", 1)) != 0:
                hallucinations["capability"]["items"].append(
                    {
                        "type": "ddp_expected_ok_overclaim",
                        "reported": True,
                        "observed": False,
                        "detail": "ddp_expected_ok=true but multi_gpu stage failed",
                    }
                )

    # Finalize counts and status.
    for k in ("path", "version", "capability"):
        hallucinations[k]["count"] = len(hallucinations[k]["items"])

    has_hallucination = any(hallucinations[k]["count"] > 0 for k in hallucinations)
    if hallucinations["path"]["count"] > 0:
        failure_category = "path_hallucination"
    elif hallucinations["version"]["count"] > 0:
        failure_category = "version_hallucination"
    elif hallucinations["capability"]["count"] > 0:
        failure_category = "capability_hallucination"
    else:
        failure_category = "unknown"

    status = "failure" if has_hallucination else "success"
    exit_code = 1 if has_hallucination else 0

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
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
        "meta": {
            "git_commit": get_git_commit(root),
            "timestamp_utc": utc_now_iso(),
        },
        "failure_category": failure_category if status == "failure" else "unknown",
        "error_excerpt": tail_lines(log_path),
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
