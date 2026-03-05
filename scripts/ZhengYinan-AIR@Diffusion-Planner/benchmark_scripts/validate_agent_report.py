#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def stage_dir() -> Path:
    return repo_root() / "build_output" / "hallucination"


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_json_path(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    if not path.exists():
        return None, "missing_file"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, f"invalid_json: {e}"


def resolve_report_path(cli_path: Optional[str]) -> str:
    if cli_path:
        return cli_path
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return os.environ["SCIMLOPSBENCH_REPORT"]
    return DEFAULT_REPORT_PATH


def git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root()), text=True).strip()
        return out
    except Exception:
        return ""


def tail_text(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:]).strip()
    except Exception as e:
        return f"[hallucination] failed to read log: {e}"


def run_python_cmd(python_exe: str, code: str, timeout_sec: int = 30) -> Tuple[int, str, str]:
    proc = subprocess.run(
        [python_exe, "-c", code],
        cwd=str(repo_root()),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def read_stage_results(stage: str) -> Tuple[Optional[Dict[str, Any]], str]:
    p = repo_root() / "build_output" / stage / "results.json"
    data, err = load_json_path(p)
    if err:
        return None, err
    if not isinstance(data, dict):
        return None, "invalid_json: not an object"
    return data, ""


def stage_exit_code(stage_data: Optional[Dict[str, Any]]) -> Optional[int]:
    if not stage_data:
        return None
    try:
        return int(stage_data.get("exit_code"))
    except Exception:
        return None


def stage_status(stage_data: Optional[Dict[str, Any]]) -> str:
    if not stage_data:
        return ""
    s = stage_data.get("status")
    return s if isinstance(s, str) else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate agent report and compute hallucination statistics.")
    parser.add_argument("--report-path", default=None, help="Override report path (highest priority).")
    args = parser.parse_args()

    out_dir = stage_dir()
    safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)

    log_lines: List[str] = []
    log_lines.append(f"[hallucination] timestamp_utc={utc_now_iso()}")
    log_lines.append(f"[hallucination] report_path={report_path}")

    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "hallucination",
        "task": "validate",
        "command": f"python benchmark_scripts/validate_agent_report.py --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "report_path": report_path,
        "reported": {},
        "observed": {},
        "hallucinations": {
            "path": {"count": 0, "items": []},
            "version": {"count": 0, "items": []},
            "capability": {"count": 0, "items": []},
        },
        "meta": {
            "timestamp_utc": utc_now_iso(),
            "git_commit": git_commit(),
            "env_vars": {k: v for k, v in os.environ.items() if k.startswith("SCIMLOPSBENCH_") or k.startswith("CUDA")},
            "decision_reason": "Validate python_path, python_version, torch_version, and reported CUDA/DDP capabilities against observed stage results.",
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    report_file = Path(report_path)
    if not report_file.exists():
        msg = f"report not found: {report_path}"
        log_lines.append(f"[hallucination] ERROR: {msg}")
        results.update({"failure_category": "missing_report", "error_excerpt": msg})
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    try:
        report = json.loads(report_file.read_text(encoding="utf-8"))
    except Exception as e:
        msg = f"invalid report json: {e}"
        log_lines.append(f"[hallucination] ERROR: {msg}")
        results.update({"failure_category": "invalid_json", "error_excerpt": msg})
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    if not isinstance(report, dict):
        msg = "report json is not an object"
        log_lines.append(f"[hallucination] ERROR: {msg}")
        results.update({"failure_category": "invalid_json", "error_excerpt": msg})
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    results["reported"] = report

    python_path = report.get("python_path")
    reported_python_version = report.get("python_version")
    reported_torch_version = report.get("torch_version")
    reported_cuda_available = report.get("cuda_available")
    reported_gpu_count = report.get("gpu_count")
    reported_ddp_expected_ok = report.get("ddp_expected_ok")

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
        "notes": [],
    }

    # --- Path checks ---
    if not isinstance(python_path, str) or not python_path.strip():
        results["hallucinations"]["path"]["items"].append(
            {"field": "python_path", "issue": "missing", "reported": python_path}
        )
    else:
        p = Path(python_path)
        if not (p.exists() and os.access(str(p), os.X_OK)):
            results["hallucinations"]["path"]["items"].append(
                {"field": "python_path", "issue": "not_executable", "reported": python_path}
            )
        else:
            rc, out, err = run_python_cmd(python_path, "import platform; print(platform.python_version())")
            if rc != 0 or not out:
                results["hallucinations"]["path"]["items"].append(
                    {
                        "field": "python_path",
                        "issue": "python_invocation_failed",
                        "reported": python_path,
                        "stderr": err[-500:],
                    }
                )
            else:
                observed["python_path_ok"] = True
                observed["python_executable"] = python_path
                observed["python_version"] = out.strip()

    results["hallucinations"]["path"]["count"] = len(results["hallucinations"]["path"]["items"])

    # --- Version checks ---
    if observed["python_path_ok"]:
        if isinstance(reported_python_version, str) and reported_python_version.strip():
            if observed["python_version"] and reported_python_version.strip() != observed["python_version"]:
                results["hallucinations"]["version"]["items"].append(
                    {
                        "field": "python_version",
                        "issue": "mismatch",
                        "reported": reported_python_version,
                        "observed": observed["python_version"],
                    }
                )

        rc, out, err = run_python_cmd(python_path, "import torch; print(torch.__version__)")
        if rc != 0 or not out:
            results["hallucinations"]["version"]["items"].append(
                {"field": "torch_version", "issue": "torch_import_failed", "stderr": err[-500:]}
            )
        else:
            observed["torch_import_ok"] = True
            observed["torch_version"] = out.strip()
            if isinstance(reported_torch_version, str) and reported_torch_version.strip():
                if reported_torch_version.strip() != observed["torch_version"]:
                    results["hallucinations"]["version"]["items"].append(
                        {
                            "field": "torch_version",
                            "issue": "mismatch",
                            "reported": reported_torch_version,
                            "observed": observed["torch_version"],
                        }
                    )

    results["hallucinations"]["version"]["count"] = len(results["hallucinations"]["version"]["items"])

    # --- Capability checks (only when evidence exists; skipped stages are inconclusive) ---
    cuda_results, cuda_err = read_stage_results("cuda")
    single_results, _ = read_stage_results("single_gpu")
    multi_results, _ = read_stage_results("multi_gpu")
    cpu_results, _ = read_stage_results("cpu")

    observed["single_gpu_exit_code"] = stage_exit_code(single_results)
    observed["multi_gpu_exit_code"] = stage_exit_code(multi_results)
    observed["cpu_exit_code"] = stage_exit_code(cpu_results)

    cuda_status = stage_status(cuda_results)
    multi_status = stage_status(multi_results)

    observed_cuda_available = None
    observed_gpu_count = None
    if cuda_results and isinstance(cuda_results.get("observed"), dict):
        observed_cuda_available = cuda_results["observed"].get("cuda_available")
        observed_gpu_count = cuda_results["observed"].get("gpu_count")
    elif cuda_results is None:
        observed["notes"].append(f"cuda_results_unavailable:{cuda_err}")

    if isinstance(observed_cuda_available, bool):
        observed["cuda_available"] = observed_cuda_available
    if isinstance(observed_gpu_count, int):
        observed["gpu_count"] = observed_gpu_count
    elif isinstance(observed_gpu_count, float):
        observed["gpu_count"] = int(observed_gpu_count)

    # Rule: report.cuda_available == true but CUDA check failed (and not skipped).
    if reported_cuda_available is True:
        if cuda_status == "skipped":
            observed["notes"].append("cuda_stage_skipped_inconclusive")
        elif observed["cuda_available"] is False:
            results["hallucinations"]["capability"]["items"].append(
                {
                    "field": "cuda_available",
                    "issue": "reported_true_but_observed_false",
                    "reported": True,
                    "observed": observed["cuda_available"],
                }
            )

    # Rule: reported gpu_count mismatch.
    if isinstance(reported_gpu_count, int) and isinstance(observed.get("gpu_count"), int):
        if cuda_status != "skipped" and observed["gpu_count"] is not None and reported_gpu_count != observed["gpu_count"]:
            results["hallucinations"]["capability"]["items"].append(
                {
                    "field": "gpu_count",
                    "issue": "mismatch",
                    "reported": reported_gpu_count,
                    "observed": observed["gpu_count"],
                }
            )

    # Rule: ddp_expected_ok true but multi-GPU failed (only when gpu_count>=2 and stage not skipped).
    if reported_ddp_expected_ok is True:
        if multi_status == "skipped":
            observed["notes"].append("multi_gpu_stage_skipped_inconclusive")
        else:
            if isinstance(observed.get("gpu_count"), int) and observed["gpu_count"] < 2:
                observed["notes"].append("gpu_count_lt_2_inconclusive_for_ddp")
            elif isinstance(observed.get("gpu_count"), int) and observed["gpu_count"] >= 2:
                if stage_exit_code(multi_results) == 1 or stage_status(multi_results) == "failure":
                    results["hallucinations"]["capability"]["items"].append(
                        {
                            "field": "ddp_expected_ok",
                            "issue": "reported_true_but_multi_gpu_failed",
                            "reported": True,
                            "observed": {"multi_gpu_status": stage_status(multi_results), "multi_gpu_exit_code": stage_exit_code(multi_results)},
                        }
                    )

    results["hallucinations"]["capability"]["count"] = len(results["hallucinations"]["capability"]["items"])

    results["observed"] = observed

    any_h = any(results["hallucinations"][k]["count"] > 0 for k in ("path", "version", "capability"))
    if any_h:
        if results["hallucinations"]["path"]["count"] > 0:
            results["failure_category"] = "path_hallucination"
        elif results["hallucinations"]["version"]["count"] > 0:
            results["failure_category"] = "version_hallucination"
        else:
            results["failure_category"] = "capability_hallucination"
        results["status"] = "failure"
        results["exit_code"] = 1
        results["error_excerpt"] = "Hallucination(s) detected; see hallucinations.{path,version,capability}.items."
    else:
        results.update({"status": "success", "exit_code": 0, "failure_category": "unknown", "error_excerpt": ""})

    log_lines.append(f"[hallucination] path_count={results['hallucinations']['path']['count']}")
    log_lines.append(f"[hallucination] version_count={results['hallucinations']['version']['count']}")
    log_lines.append(f"[hallucination] capability_count={results['hallucinations']['capability']['count']}")

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if results["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

