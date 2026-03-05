#!/usr/bin/env python3
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


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def tail(path: Path, max_lines: int = 240) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-max_lines:])


def try_git_commit(root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def resolve_report_path(cli_path: Optional[str]) -> str:
    if cli_path:
        return cli_path
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return env_path
    return DEFAULT_REPORT_PATH


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, f"missing_file: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "json_not_object"
        return data, None
    except Exception as e:
        return None, f"invalid_json: {e}"


def run_python_cmd(python_executable: str, code: str, timeout_sec: int = 60) -> Tuple[int, str, str]:
    try:
        cp = subprocess.run(
            [python_executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        return int(cp.returncode), cp.stdout.strip(), cp.stderr.strip()
    except Exception as e:
        return 999, "", f"subprocess_failed: {e}"


def read_stage_results(root: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], str]:
    p = root / "build_output" / stage / "results.json"
    data, err = load_json(p)
    if data is None:
        return None, f"{stage}: {err}"
    return data, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate /opt/scimlopsbench/report.json and compute hallucination stats.")
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report_file = Path(report_path)
    report, report_err = load_json(report_file)

    hallucinations: Dict[str, Any] = {
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
        "cuda_available": False,
        "gpu_count": 0,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
        "cpu_exit_code": None,
    }

    meta: Dict[str, Any] = {
        "python": f"{sys.executable} ({platform.python_version()})",
        "git_commit": try_git_commit(root),
        "timestamp_utc": utc_now_iso(),
        "warnings": [],
    }

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[hallucination] timestamp_utc={utc_now_iso()}\n")
        log_f.write(f"[hallucination] report_path={report_path}\n")

        if report is None:
            log_f.write(f"[hallucination] report_error={report_err}\n")
            failure_category = "missing_report" if report_err and "missing_file" in report_err else "invalid_json"
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "hallucination",
                "task": "validate",
                "command": f"{sys.executable} benchmark_scripts/validate_agent_report.py --report-path {report_path}",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "report_path": report_path,
                "reported": {},
                "observed": observed,
                "hallucinations": hallucinations,
                "failure_category": failure_category,
                "error_excerpt": tail(log_path),
            }
            results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 1

        reported = report
        python_path = str(reported.get("python_path") or "")
        observed["python_executable"] = python_path

        # Path hallucinations
        if not python_path:
            hallucinations["path"]["items"].append(
                {"type": "python_path_missing", "reported": {"python_path": python_path}, "observed": {}}
            )
        elif not Path(python_path).exists() or not os.access(python_path, os.X_OK):
            hallucinations["path"]["items"].append(
                {"type": "python_path_not_executable", "reported": {"python_path": python_path}, "observed": {}}
            )
        else:
            rc, out, err = run_python_cmd(python_path, "import platform; print(platform.python_version())", timeout_sec=30)
            if rc != 0 or not out:
                hallucinations["path"]["items"].append(
                    {
                        "type": "python_path_unusable",
                        "reported": {"python_path": python_path},
                        "observed": {"returncode": rc, "stderr": err},
                    }
                )
            else:
                observed["python_path_ok"] = True
                observed["python_version"] = out

        # Version hallucinations
        reported_py_ver = str(reported.get("python_version") or "")
        if observed["python_path_ok"] and reported_py_ver and reported_py_ver != observed["python_version"]:
            hallucinations["version"]["items"].append(
                {
                    "type": "python_version_mismatch",
                    "reported": {"python_version": reported_py_ver},
                    "observed": {"python_version": observed["python_version"]},
                }
            )

        reported_torch_ver = str(reported.get("torch_version") or "")
        if observed["python_path_ok"] and reported_torch_ver:
            rc, out, err = run_python_cmd(python_path, "import torch; print(torch.__version__)", timeout_sec=60)
            if rc == 0 and out:
                observed["torch_import_ok"] = True
                observed["torch_version"] = out
            else:
                hallucinations["version"]["items"].append(
                    {
                        "type": "torch_import_failed",
                        "reported": {"torch_version": reported_torch_ver},
                        "observed": {"stderr": err, "returncode": rc},
                    }
                )
        if observed["torch_import_ok"] and reported_torch_ver and reported_torch_ver != observed["torch_version"]:
            hallucinations["version"]["items"].append(
                {
                    "type": "torch_version_mismatch",
                    "reported": {"torch_version": reported_torch_ver},
                    "observed": {"torch_version": observed["torch_version"]},
                }
            )

        # Observed benchmark evidence (only from stage results; missing/invalid => inconclusive)
        cuda_res, cuda_err = read_stage_results(root, "cuda")
        if cuda_res is None:
            meta["warnings"].append(cuda_err)
        else:
            obs = cuda_res.get("observed", {}) if isinstance(cuda_res, dict) else {}
            if isinstance(obs, dict):
                observed["cuda_available"] = bool(obs.get("cuda_available", False))
                try:
                    observed["gpu_count"] = int(obs.get("gpu_count", 0))
                except Exception:
                    observed["gpu_count"] = 0

        cpu_res, cpu_err = read_stage_results(root, "cpu")
        if cpu_res is None:
            meta["warnings"].append(cpu_err)
        else:
            observed["cpu_exit_code"] = cpu_res.get("exit_code")

        single_res, single_err = read_stage_results(root, "single_gpu")
        if single_res is None:
            meta["warnings"].append(single_err)
        else:
            observed["single_gpu_exit_code"] = single_res.get("exit_code")

        multi_res, multi_err = read_stage_results(root, "multi_gpu")
        if multi_res is None:
            meta["warnings"].append(multi_err)
        else:
            observed["multi_gpu_exit_code"] = multi_res.get("exit_code")

        # Capability hallucinations (judge only if relevant stage evidence exists and not skipped)
        reported_cuda = reported.get("cuda_available")
        if isinstance(reported_cuda, bool) and cuda_res is not None:
            cuda_status = str(cuda_res.get("status", ""))
            cuda_exit = int(cuda_res.get("exit_code", 1))
            if reported_cuda is True and (cuda_status == "failure" or cuda_exit == 1):
                hallucinations["capability"]["items"].append(
                    {"type": "cuda_available_overclaim", "reported": {"cuda_available": True}, "observed": {"cuda_stage_status": cuda_status, "cuda_stage_exit_code": cuda_exit}}
                )

        reported_gpu_count = reported.get("gpu_count")
        if isinstance(reported_gpu_count, int) and cuda_res is not None:
            if observed["gpu_count"] != reported_gpu_count:
                hallucinations["capability"]["items"].append(
                    {"type": "gpu_count_mismatch", "reported": {"gpu_count": reported_gpu_count}, "observed": {"gpu_count": observed["gpu_count"]}}
                )

        ddp_expected_ok = reported.get("ddp_expected_ok")
        if isinstance(ddp_expected_ok, bool):
            if multi_res is None:
                meta["warnings"].append("multi_gpu_results_missing_inconclusive")
            else:
                multi_status = str(multi_res.get("status", ""))
                multi_exit = int(multi_res.get("exit_code", 0))
                if multi_status == "skipped":
                    meta["warnings"].append("multi_gpu_stage_skipped_inconclusive")
                else:
                    if ddp_expected_ok is True:
                        if observed.get("gpu_count", 0) >= 2:
                            if multi_status == "failure" or multi_exit == 1:
                                hallucinations["capability"]["items"].append(
                                    {
                                        "type": "ddp_expected_ok_but_failed",
                                        "reported": {"ddp_expected_ok": True},
                                        "observed": {"gpu_count": observed.get("gpu_count", 0), "multi_gpu_exit_code": multi_exit},
                                    }
                                )
                        else:
                            meta["warnings"].append("ddp_expected_ok_inconclusive_gpu_count_lt_2")
                    else:
                        # Optional: underclaim
                        if multi_status == "success" and multi_exit == 0:
                            hallucinations["capability"]["items"].append(
                                {
                                    "type": "ddp_underclaim",
                                    "reported": {"ddp_expected_ok": False},
                                    "observed": {"multi_gpu_exit_code": multi_exit},
                                }
                            )

        for k in ("path", "version", "capability"):
            hallucinations[k]["count"] = len(hallucinations[k]["items"])

        any_hallucination = any(hallucinations[k]["count"] > 0 for k in hallucinations)
        status = "failure" if any_hallucination else "success"
        exit_code = 1 if any_hallucination else 0

        failure_category = ""
        if hallucinations["path"]["count"] > 0:
            failure_category = "path_hallucination"
        elif hallucinations["version"]["count"] > 0:
            failure_category = "version_hallucination"
        elif hallucinations["capability"]["count"] > 0:
            failure_category = "capability_hallucination"

        payload = {
            "status": status,
            "skip_reason": "unknown",
            "exit_code": exit_code,
            "stage": "hallucination",
            "task": "validate",
            "command": f"{sys.executable} benchmark_scripts/validate_agent_report.py --report-path {report_path}",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "report_path": report_path,
            "reported": reported,
            "observed": observed,
            "hallucinations": hallucinations,
            "meta": meta,
            "failure_category": failure_category,
            "error_excerpt": tail(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
