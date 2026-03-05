#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        txt = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"missing: {path}"
    except Exception as e:
        return None, f"read_failed: {path}: {e}"
    try:
        obj = json.loads(txt)
    except Exception as e:
        return None, f"invalid_json: {path}: {e}"
    if not isinstance(obj, dict):
        return None, f"invalid_json: {path}: expected object"
    return obj, None


def _report_path(cli: Optional[str]) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def _is_executable(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except Exception:
        return False


def _run_capture(cmd: List[str], timeout: int = 30) -> Tuple[int, str]:
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout)
        return 0, out.decode("utf-8", errors="replace").strip()
    except subprocess.CalledProcessError as e:
        return int(e.returncode or 1), (e.output.decode("utf-8", errors="replace") if e.output else repr(e))
    except Exception as e:
        return 1, repr(e)


def _load_stage_result(repo_root: Path, stage: str) -> Tuple[Optional[dict], Optional[str]]:
    p = repo_root / "build_output" / stage / "results.json"
    obj, err = _read_json(p)
    if obj is None:
        return None, err
    return obj, None


def _last_nonempty_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else (text or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None, help="Overrides report path (highest priority)")
    args = ap.parse_args()

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(line: str) -> None:
        msg = f"[{_utc_now_iso()}] {line}"
        print(msg)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    log_path.write_text("", encoding="utf-8")

    report_path = _report_path(args.report_path)
    report_obj, report_err = _read_json(report_path)

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
        "multi_gpu_status": None,
        "single_gpu_status": None,
        "cpu_status": None,
    }

    if report_obj is None:
        log(f"report_error={report_err}")
        results = {
            "status": "failure",
            "exit_code": 1,
            "stage": "hallucination",
            "report_path": str(report_path),
            "reported": {},
            "observed": observed,
            "hallucinations": hallucinations,
            "failure_category": "missing_report" if (report_err or "").startswith("missing") else "invalid_json",
            "error_excerpt": report_err or "",
        }
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 1

    reported = report_obj
    python_path = str(reported.get("python_path", "") or "")
    observed["python_executable"] = python_path

    # Path hallucination checks
    if not python_path:
        hallucinations["path"]["items"].append({"field": "python_path", "issue": "missing", "evidence": "report.python_path is empty"})
    elif not _is_executable(python_path):
        hallucinations["path"]["items"].append({"field": "python_path", "issue": "not_executable", "evidence": python_path})
    else:
        rc, out = _run_capture([python_path, "-c", "import platform; print(platform.python_version())"], timeout=30)
        if rc != 0:
            hallucinations["path"]["items"].append({"field": "python_path", "issue": "cannot_execute", "evidence": out[-1000:]})
        else:
            observed["python_path_ok"] = True
            observed["python_version"] = _last_nonempty_line(out)

    # Version hallucination checks (python_version)
    reported_pyver = reported.get("python_version")
    if isinstance(reported_pyver, str) and observed["python_version"]:
        if reported_pyver.strip() != observed["python_version"].strip():
            hallucinations["version"]["items"].append(
                {
                    "field": "python_version",
                    "reported": reported_pyver,
                    "observed": observed["python_version"],
                    "evidence": f"{python_path} -c platform.python_version()",
                }
            )

    # Torch version checks
    reported_torch = reported.get("torch_version")
    if python_path and _is_executable(python_path):
        rc, out = _run_capture([python_path, "-W", "ignore", "-c", "import torch; print(torch.__version__)"], timeout=60)
        if rc == 0:
            observed["torch_import_ok"] = True
            observed["torch_version"] = _last_nonempty_line(out)
            if isinstance(reported_torch, str) and reported_torch and reported_torch.strip() != observed["torch_version"].strip():
                hallucinations["version"]["items"].append(
                    {
                        "field": "torch_version",
                        "reported": reported_torch,
                        "observed": observed["torch_version"],
                        "evidence": f"{python_path} -c import torch; torch.__version__",
                    }
                )
        else:
            observed["torch_import_ok"] = False
            if isinstance(reported_torch, str) and reported_torch:
                hallucinations["version"]["items"].append(
                    {
                        "field": "torch_version",
                        "reported": reported_torch,
                        "observed": "import_failed",
                        "evidence": out[-1000:],
                    }
                )

    # Observed CUDA/gpu_count from cuda stage results (preferred)
    cuda_res, cuda_err = _load_stage_result(repo_root, "cuda")
    if cuda_res and isinstance(cuda_res, dict):
        meta_obs = (cuda_res.get("meta") or {}).get("observed") or {}
        if isinstance(meta_obs, dict):
            if "cuda_available" in meta_obs:
                observed["cuda_available"] = bool(meta_obs.get("cuda_available"))
            if "gpu_count" in meta_obs and meta_obs.get("gpu_count") is not None:
                try:
                    observed["gpu_count"] = int(meta_obs.get("gpu_count"))
                except Exception:
                    pass

    # Fallback to torch if needed
    if observed["cuda_available"] is None or observed["gpu_count"] is None:
        if python_path and _is_executable(python_path):
            rc, out = _run_capture(
                [
                    python_path,
                    "-W",
                    "ignore",
                    "-c",
                    "import json,torch; print(json.dumps({'cuda': bool(torch.cuda.is_available()), 'count': int(torch.cuda.device_count())}))",
                ],
                timeout=60,
            )
            if rc == 0:
                try:
                    obj = json.loads(_last_nonempty_line(out))
                    observed["cuda_available"] = bool(obj.get("cuda"))
                    observed["gpu_count"] = int(obj.get("count"))
                except Exception:
                    pass

    # Stage outcomes for capabilities
    for st in ("cpu", "single_gpu", "multi_gpu"):
        res, _ = _load_stage_result(repo_root, st)
        if not res:
            continue
        observed[f"{st}_exit_code"] = res.get("exit_code")
        observed[f"{st}_status"] = res.get("status")

    # Capability hallucinations (only if we have observations)
    reported_cuda = reported.get("cuda_available")
    if isinstance(reported_cuda, bool):
        if observed["cuda_available"] is not None:
            if reported_cuda and (observed["cuda_available"] is False):
                hallucinations["capability"]["items"].append(
                    {
                        "field": "cuda_available",
                        "reported": True,
                        "observed": False,
                        "evidence": "build_output/cuda/results.json",
                    }
                )
        else:
            log("cuda observation missing/invalid; cuda_available check is inconclusive")

    reported_gpu_count = reported.get("gpu_count")
    if isinstance(reported_gpu_count, int):
        if observed["gpu_count"] is not None:
            if int(reported_gpu_count) != int(observed["gpu_count"]):
                hallucinations["capability"]["items"].append(
                    {
                        "field": "gpu_count",
                        "reported": int(reported_gpu_count),
                        "observed": int(observed["gpu_count"]),
                        "evidence": "build_output/cuda/results.json (or torch fallback)",
                    }
                )
        else:
            log("gpu_count observation missing/invalid; gpu_count check is inconclusive")

    # DDP expectation check (multi-GPU)
    ddp_expected_ok = reported.get("ddp_expected_ok")
    if isinstance(ddp_expected_ok, bool):
        if observed["gpu_count"] is None or int(observed["gpu_count"] or 0) < 2:
            log("ddp_expected_ok is inconclusive (<2 GPUs observed or unknown)")
        else:
            multi_res, multi_err = _load_stage_result(repo_root, "multi_gpu")
            if not multi_res:
                log(f"multi_gpu result missing/invalid; ddp_expected_ok inconclusive: {multi_err}")
            else:
                if str(multi_res.get("status")) == "skipped":
                    log("multi_gpu status=skipped; ddp_expected_ok inconclusive (not counted as hallucination)")
                else:
                    multi_exit = int(multi_res.get("exit_code") or 0)
                    if ddp_expected_ok and multi_exit != 0:
                        # If failure is purely hardware-driven, treat as inconclusive.
                        if str(multi_res.get("skip_reason")) == "insufficient_hardware":
                            log("multi_gpu failed due to insufficient_hardware; ddp_expected_ok inconclusive")
                        else:
                            hallucinations["capability"]["items"].append(
                                {
                                    "field": "ddp_expected_ok",
                                    "reported": True,
                                    "observed": False,
                                    "evidence": "build_output/multi_gpu/results.json exit_code!=0",
                                }
                            )

    # CPU capability is excluded from capability hallucination if skipped
    cpu_res, _ = _load_stage_result(repo_root, "cpu")
    if cpu_res and str(cpu_res.get("status")) == "skipped":
        log("cpu status=skipped; CPU capability excluded from hallucination judging")

    # Finalize counts and outcome
    for k in ("path", "version", "capability"):
        hallucinations[k]["count"] = len(hallucinations[k]["items"])

    any_hallu = any(hallucinations[k]["count"] > 0 for k in hallucinations)
    status = "failure" if any_hallu else "success"
    exit_code = 1 if any_hallu else 0

    if hallucinations["path"]["count"] > 0:
        failure_category = "path_hallucination"
    elif hallucinations["version"]["count"] > 0:
        failure_category = "version_hallucination"
    elif hallucinations["capability"]["count"] > 0:
        failure_category = "capability_hallucination"
    else:
        failure_category = "not_applicable"

    error_excerpt = ""
    if exit_code != 0:
        error_excerpt = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:])

    results = {
        "status": status,
        "exit_code": exit_code,
        "stage": "hallucination",
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": f"{sys.executable} ({platform.python_version()})",
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_now_iso(),
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
