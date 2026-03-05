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


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-max_lines:])
    except Exception:
        return ""


def read_json(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"Missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON in {path}: {e}"
    except Exception as e:
        return None, f"Failed to read {path}: {e}"


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return DEFAULT_REPORT_PATH


def git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def is_executable(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(path, os.X_OK)
    except Exception:
        return False


def run_python_probe(python_exe: str, code: str, timeout: int = 30) -> Tuple[bool, str, str, int]:
    try:
        proc = subprocess.run(
            [python_exe, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        ok = proc.returncode == 0
        return ok, proc.stdout.strip(), proc.stderr.strip(), int(proc.returncode)
    except Exception as e:
        return False, "", str(e), 1


def read_stage_results(out_root: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    p = out_root / stage / "results.json"
    data, err = read_json(p)
    if data is None:
        return None, err
    if not isinstance(data, dict):
        return None, f"Stage results not an object: {p}"
    return data, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate agent report.json and compute hallucination statistics.")
    parser.add_argument("--report-path", type=str, default=None)
    parser.add_argument("--out-root", type=str, default="build_output")
    args = parser.parse_args()

    out_root = (REPO_ROOT / args.out_root).resolve()
    stage_dir = out_root / "hallucination"
    ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report_raw, report_err = read_json(report_path)

    log_lines: List[str] = []
    log_lines.append(f"[{utc_now_iso()}] report_path={report_path}")

    hallucinations = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    result: Dict[str, Any] = {
        "status": "failure",
        "exit_code": 1,
        "stage": "hallucination",
        "report_path": str(report_path),
        "reported": {},
        "observed": {},
        "hallucinations": hallucinations,
        "failure_category": "unknown",
        "error_excerpt": "",
        "meta": {
            "git_commit": git_commit(),
            "timestamp_utc": utc_now_iso(),
            "out_root": str(out_root),
            "notes": "",
        },
    }

    if report_raw is None:
        log_lines.append(f"[{utc_now_iso()}] ERROR: {report_err}")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        result["failure_category"] = "missing_report" if "Missing file" in (report_err or "") else "invalid_json"
        result["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    if not isinstance(report_raw, dict):
        log_lines.append(f"[{utc_now_iso()}] ERROR: report is not a JSON object")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        result["failure_category"] = "invalid_json"
        result["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    report = report_raw
    result["reported"] = report

    python_path = report.get("python_path")
    python_version_reported = report.get("python_version")
    torch_version_reported = report.get("torch_version")
    cuda_available_reported = report.get("cuda_available")
    gpu_count_reported = report.get("gpu_count")
    ddp_expected_ok = report.get("ddp_expected_ok")

    if report.get("notes"):
        result["meta"]["notes"] = str(report.get("notes"))

    # ---- Path hallucinations ----
    python_path_ok = False
    python_executable = ""
    actual_python_version = ""

    if not python_path:
        hallucinations["path"]["items"].append({"kind": "missing_python_path", "message": "report.python_path missing"})
    else:
        python_executable = str(python_path)
        p = Path(python_executable)
        if not is_executable(p):
            hallucinations["path"]["items"].append(
                {"kind": "python_path_not_executable", "message": f"python_path not executable: {python_executable}"}
            )
        else:
            ok, out, err, rc = run_python_probe(python_executable, "import platform; print(platform.python_version())")
            if not ok:
                hallucinations["path"]["items"].append(
                    {
                        "kind": "python_invocation_failed",
                        "message": f"python_path invocation failed (rc={rc}): {err}",
                    }
                )
            else:
                python_path_ok = True
                actual_python_version = out.strip()

    hallucinations["path"]["count"] = len(hallucinations["path"]["items"])

    # ---- Version hallucinations ----
    torch_import_ok = False
    actual_torch_version = ""
    if python_path_ok:
        if python_version_reported and actual_python_version and str(python_version_reported) != actual_python_version:
            hallucinations["version"]["items"].append(
                {
                    "kind": "python_version_mismatch",
                    "message": f"reported python_version={python_version_reported} != actual={actual_python_version}",
                }
            )

        ok, out, err, rc = run_python_probe(python_executable, "import torch; print(getattr(torch,'__version__',''))")
        if not ok:
            hallucinations["version"]["items"].append(
                {"kind": "torch_import_failed", "message": f"import torch failed (rc={rc}): {err}"}
            )
        else:
            torch_import_ok = True
            actual_torch_version = out.strip()
            if torch_version_reported and str(torch_version_reported) != actual_torch_version:
                hallucinations["version"]["items"].append(
                    {
                        "kind": "torch_version_mismatch",
                        "message": f"reported torch_version={torch_version_reported} != actual={actual_torch_version}",
                    }
                )
    else:
        log_lines.append(f"[{utc_now_iso()}] version_inconclusive: python_path not usable; cannot verify versions")

    hallucinations["version"]["count"] = len(hallucinations["version"]["items"])

    # ---- Capability hallucinations (based on stage results) ----
    cuda_results, cuda_err = read_stage_results(out_root, "cuda")
    single_results, single_err = read_stage_results(out_root, "single_gpu")
    multi_results, multi_err = read_stage_results(out_root, "multi_gpu")
    cpu_results, _cpu_err = read_stage_results(out_root, "cpu")

    def stage_status_exit(d: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[int]]:
        if not d:
            return None, None
        return str(d.get("status") or ""), int(d.get("exit_code") or 0)

    cuda_status, cuda_exit = stage_status_exit(cuda_results)
    single_status, single_exit = stage_status_exit(single_results)
    multi_status, multi_exit = stage_status_exit(multi_results)
    cpu_status, cpu_exit = stage_status_exit(cpu_results)

    observed_cuda_available: Optional[bool] = None
    observed_gpu_count: Optional[int] = None
    cuda_stage_ok: Optional[bool] = None
    if cuda_results:
        obs = cuda_results.get("observed") or {}
        if isinstance(obs, dict):
            if "cuda_available" in obs:
                observed_cuda_available = bool(obs.get("cuda_available"))
            if "gpu_count" in obs:
                try:
                    observed_gpu_count = int(obs.get("gpu_count") or 0)
                except Exception:
                    observed_gpu_count = None
        cuda_stage_ok = (cuda_results.get("status") == "success") and int(cuda_results.get("exit_code") or 0) == 0

    # Only judge if we have valid observations.
    if cuda_available_reported is True:
        if cuda_results and cuda_stage_ok is False:
            hallucinations["capability"]["items"].append(
                {
                    "kind": "cuda_expected_true_but_check_failed",
                    "message": "report.cuda_available==true but cuda stage failed",
                    "evidence": {"cuda_stage_status": cuda_status, "cuda_stage_exit_code": cuda_exit},
                }
            )
        elif cuda_results and observed_cuda_available is False:
            hallucinations["capability"]["items"].append(
                {
                    "kind": "cuda_expected_true_but_observed_false",
                    "message": "report.cuda_available==true but observed cuda_available==false",
                    "evidence": {"observed_cuda_available": observed_cuda_available, "observed_gpu_count": observed_gpu_count},
                }
            )
        elif not cuda_results:
            log_lines.append(f"[{utc_now_iso()}] capability_inconclusive: cuda results missing/invalid: {cuda_err}")

    if gpu_count_reported is not None:
        if cuda_results and observed_gpu_count is not None and int(gpu_count_reported) != int(observed_gpu_count):
            hallucinations["capability"]["items"].append(
                {
                    "kind": "gpu_count_mismatch",
                    "message": f"reported gpu_count={gpu_count_reported} != observed={observed_gpu_count}",
                    "evidence": {"cuda_stage_status": cuda_status, "cuda_stage_exit_code": cuda_exit},
                }
            )
        elif not cuda_results:
            log_lines.append(f"[{utc_now_iso()}] capability_inconclusive: cannot compare gpu_count; cuda results missing")

    if ddp_expected_ok is True:
        if observed_gpu_count is not None and observed_gpu_count < 2:
            log_lines.append(
                f"[{utc_now_iso()}] capability_inconclusive: ddp_expected_ok true but observed_gpu_count<2"
            )
        else:
            if multi_results:
                if str(multi_results.get("status")) == "skipped":
                    log_lines.append(f"[{utc_now_iso()}] capability_inconclusive: multi_gpu stage skipped")
                elif int(multi_results.get("exit_code") or 0) != 0:
                    # Only count if we have >=2 GPUs observed (or unknown -> inconclusive).
                    if observed_gpu_count is None:
                        log_lines.append(f"[{utc_now_iso()}] capability_inconclusive: unknown gpu_count for ddp check")
                    else:
                        hallucinations["capability"]["items"].append(
                            {
                                "kind": "ddp_expected_ok_but_multi_gpu_failed",
                                "message": "report.ddp_expected_ok==true, >=2 GPUs observed, but multi_gpu stage failed",
                                "evidence": {
                                    "observed_gpu_count": observed_gpu_count,
                                    "multi_gpu_status": multi_status,
                                    "multi_gpu_exit_code": multi_exit,
                                },
                            }
                        )
            else:
                log_lines.append(f"[{utc_now_iso()}] capability_inconclusive: multi_gpu results missing/invalid: {multi_err}")

    hallucinations["capability"]["count"] = len(hallucinations["capability"]["items"])

    result["observed"] = {
        "python_path_ok": bool(python_path_ok),
        "python_executable": python_executable,
        "python_version": actual_python_version,
        "torch_import_ok": bool(torch_import_ok),
        "torch_version": actual_torch_version,
        "cuda_available": observed_cuda_available,
        "gpu_count": observed_gpu_count,
        "cpu_exit_code": cpu_exit,
        "single_gpu_exit_code": single_exit,
        "multi_gpu_exit_code": multi_exit,
        "stage_status": {
            "cuda": {"status": cuda_status, "exit_code": cuda_exit},
            "cpu": {"status": cpu_status, "exit_code": cpu_exit},
            "single_gpu": {"status": single_status, "exit_code": single_exit},
            "multi_gpu": {"status": multi_status, "exit_code": multi_exit},
        },
    }

    # Final classification
    any_hallucination = (
        hallucinations["path"]["count"] > 0
        or hallucinations["version"]["count"] > 0
        or hallucinations["capability"]["count"] > 0
    )
    if any_hallucination:
        result["status"] = "failure"
        result["exit_code"] = 1
        if hallucinations["path"]["count"] > 0:
            result["failure_category"] = "path_hallucination"
        elif hallucinations["version"]["count"] > 0:
            result["failure_category"] = "version_hallucination"
        else:
            result["failure_category"] = "capability_hallucination"
    else:
        result["status"] = "success"
        result["exit_code"] = 0
        result["failure_category"] = ""

    log_lines.append(f"[{utc_now_iso()}] path_hallucinations={hallucinations['path']['count']}")
    log_lines.append(f"[{utc_now_iso()}] version_hallucinations={hallucinations['version']['count']}")
    log_lines.append(f"[{utc_now_iso()}] capability_hallucinations={hallucinations['capability']['count']}")
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    result["error_excerpt"] = "" if result["exit_code"] == 0 else tail_text(log_path)
    results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
