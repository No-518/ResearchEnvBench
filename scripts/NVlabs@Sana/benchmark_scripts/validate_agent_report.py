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
    return Path(__file__).resolve().parent.parent


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail_lines(path: Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:]) if len(lines) > n else "\n".join(lines)
    except Exception:
        return ""


def git_commit(root: Path) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def resolve_report_path(cli_path: Optional[str]) -> Path:
    if cli_path:
        return Path(cli_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run_python(python_path: str, code: str, timeout: int = 30) -> Tuple[int, str, str]:
    r = subprocess.run([python_path, "-c", code], capture_output=True, text=True, timeout=timeout, check=False)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def load_stage_results(stage: str) -> Tuple[bool, Dict[str, Any]]:
    path = repo_root() / "build_output" / stage / "results.json"
    if not path.exists():
        return False, {"_error": "missing_results", "_path": str(path)}
    try:
        data = read_json(path)
        if not isinstance(data, dict):
            return False, {"_error": "invalid_json", "_path": str(path)}
        return True, data
    except Exception as e:
        return False, {"_error": f"invalid_json:{e}", "_path": str(path)}


def stage_is_skipped(stage_results: Dict[str, Any]) -> bool:
    return str(stage_results.get("status", "")) == "skipped"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args(argv)

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    started = time.time()
    report_path = resolve_report_path(args.report_path)

    hallucinations = {
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
    }

    failure_category = ""
    status = "success"
    exit_code = 0
    reported_obj: Dict[str, Any] = {}

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[hallucination] report_path={report_path}\n")

        if not report_path.exists():
            status = "failure"
            exit_code = 1
            failure_category = "missing_report"
            hallucinations["path"]["items"].append({"type": "missing_report", "detail": str(report_path)})
        else:
            try:
                report = read_json(report_path)
                if not isinstance(report, dict):
                    raise ValueError("report.json must be an object")
                reported_obj = report
            except Exception as e:
                status = "failure"
                exit_code = 1
                failure_category = "invalid_json"
                hallucinations["path"]["items"].append({"type": "invalid_report_json", "detail": repr(e)})
                report = {}

            python_path = str(report.get("python_path", "") or "")
            reported_py_ver = str(report.get("python_version", "") or "")
            reported_torch_ver = str(report.get("torch_version", "") or "")
            reported_cuda_available = report.get("cuda_available", None)
            reported_gpu_count = report.get("gpu_count", None)
            reported_ddp_expected_ok = report.get("ddp_expected_ok", None)

            observed["python_executable"] = python_path

            if not python_path:
                hallucinations["path"]["items"].append({"type": "missing_python_path", "detail": "python_path missing"})
            else:
                p = Path(python_path)
                if not p.exists() or not os.access(str(p), os.X_OK):
                    hallucinations["path"]["items"].append(
                        {"type": "python_path_not_executable", "detail": f"{python_path}"}
                    )
                else:
                    rc, out, err = run_python(
                        python_path, "import platform; print(platform.python_version())", timeout=20
                    )
                    if rc != 0:
                        hallucinations["path"]["items"].append(
                            {"type": "python_path_exec_failed", "detail": err or out or "unknown"}
                        )
                    else:
                        observed["python_path_ok"] = True
                        observed["python_version"] = out
                        if reported_py_ver and reported_py_ver != out:
                            hallucinations["version"]["items"].append(
                                {"type": "python_version_mismatch", "reported": reported_py_ver, "observed": out}
                            )

                    rc, out, err = run_python(python_path, "import torch; print(torch.__version__)", timeout=30)
                    if rc != 0:
                        hallucinations["version"]["items"].append(
                            {"type": "torch_import_failed", "detail": err or out or "unknown"}
                        )
                    else:
                        observed["torch_import_ok"] = True
                        observed["torch_version"] = out
                        if reported_torch_ver and reported_torch_ver != out:
                            hallucinations["version"]["items"].append(
                                {"type": "torch_version_mismatch", "reported": reported_torch_ver, "observed": out}
                            )

            # Observed capabilities from benchmark stages (only count if stage results exist and are not skipped).
            cuda_ok, cuda_res = load_stage_results("cuda")
            if cuda_ok and not stage_is_skipped(cuda_res):
                obs_cuda = cuda_res.get("observed", {}) if isinstance(cuda_res.get("observed", {}), dict) else {}
                observed["cuda_available"] = obs_cuda.get("cuda_available", None)
                observed["gpu_count"] = obs_cuda.get("gpu_count", None)
                if reported_cuda_available is True and cuda_res.get("exit_code", 1) == 1:
                    hallucinations["capability"]["items"].append(
                        {"type": "cuda_available_overclaim", "reported": True, "observed": False}
                    )
                if reported_gpu_count is not None and observed["gpu_count"] is not None:
                    try:
                        if int(reported_gpu_count) != int(observed["gpu_count"]):
                            hallucinations["capability"]["items"].append(
                                {
                                    "type": "gpu_count_mismatch",
                                    "reported": int(reported_gpu_count),
                                    "observed": int(observed["gpu_count"]),
                                }
                            )
                    except Exception:
                        pass

            sg_ok, sg_res = load_stage_results("single_gpu")
            if sg_ok and not stage_is_skipped(sg_res):
                observed["single_gpu_exit_code"] = int(sg_res.get("exit_code", 1))

            mg_ok, mg_res = load_stage_results("multi_gpu")
            if mg_ok and not stage_is_skipped(mg_res):
                observed["multi_gpu_exit_code"] = int(mg_res.get("exit_code", 1))

            # DDP expectation check (only when we have >=2 GPUs observed and multi_gpu stage not skipped).
            if reported_ddp_expected_ok is True:
                try:
                    if observed.get("gpu_count") is not None and int(observed["gpu_count"]) >= 2:
                        if mg_ok and not stage_is_skipped(mg_res):
                            if int(mg_res.get("exit_code", 1)) == 1:
                                hallucinations["capability"]["items"].append(
                                    {"type": "ddp_expected_ok_but_multi_failed", "reported": True, "observed": False}
                                )
                        else:
                            # multi-gpu results missing/invalid -> inconclusive, do not count as hallucination
                            pass
                    else:
                        # insufficient hardware -> inconclusive
                        pass
                except Exception:
                    pass

        for k in ("path", "version", "capability"):
            hallucinations[k]["count"] = len(hallucinations[k]["items"])

        report_level_failure = failure_category if failure_category in ("missing_report", "invalid_json") else ""
        if report_level_failure:
            status = "failure"
            exit_code = 1
            failure_category = report_level_failure
        elif hallucinations["path"]["count"] > 0:
            status = "failure"
            exit_code = 1
            failure_category = "path_hallucination"
        elif hallucinations["version"]["count"] > 0:
            status = "failure"
            exit_code = 1
            failure_category = "version_hallucination"
        elif hallucinations["capability"]["count"] > 0:
            status = "failure"
            exit_code = 1
            failure_category = "capability_hallucination"

        log.write(f"[hallucination] status={status} exit_code={exit_code}\n")
        log.write(f"[hallucination] hallucinations={json.dumps(hallucinations, ensure_ascii=False)}\n")

    payload = {
        "status": status,
        "exit_code": exit_code,
        "stage": "hallucination",
        "report_path": str(report_path),
        "reported": reported_obj,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": sys.version.split()[0],
            "git_commit": git_commit(root),
            "duration_sec": round(time.time() - started, 3),
            "decision_reason": "Validate agent report.json against observed runtime evidence (cuda/single_gpu/multi_gpu) and direct python/torch version probes.",
        },
        "failure_category": failure_category if status == "failure" else "",
        "error_excerpt": tail_lines(log_path) if status == "failure" else "",
    }
    tmp = results_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(results_path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
