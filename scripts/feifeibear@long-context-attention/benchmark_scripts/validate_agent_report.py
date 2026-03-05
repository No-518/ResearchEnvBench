#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root),
                text=True,
                stderr=subprocess.DEVNULL,
            )
            .strip()
            .strip()
        )
    except Exception:
        return ""


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def resolve_report_path(cli_path: str) -> Path:
    if cli_path:
        return Path(cli_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return DEFAULT_REPORT_PATH


def read_stage_results(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, f"invalid_json:{e}"


def run_python(python: str, code: str, timeout: int = 30) -> Tuple[bool, str]:
    try:
        p = subprocess.run(
            [python, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if p.returncode != 0:
            return False, (p.stderr or p.stdout).strip()
        return True, p.stdout.strip()
    except Exception as e:
        return False, str(e)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    cmd_str = " ".join(shlex.quote(a) for a in sys.argv)
    report_path = resolve_report_path(args.report_path)

    env_vars = {
        "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
        "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }

    hallucinations: Dict[str, Dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    def add(kind: str, item: Dict[str, Any]) -> None:
        hallucinations[kind]["items"].append(item)
        hallucinations[kind]["count"] = len(hallucinations[kind]["items"])

    status = "success"
    failure_category = "unknown"
    exit_code = 0
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
        "capability_notes": {},
    }

    # Load report
    try:
        reported = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        status = "failure"
        exit_code = 1
        failure_category = "missing_report"
        error_excerpt = f"Missing report.json at {report_path}"
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"[hallucination] {error_excerpt}\n")
        payload = {
            "status": status,
            "skip_reason": "unknown",
            "exit_code": exit_code,
            "stage": "hallucination",
            "task": "validate",
            "command": cmd_str,
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {
                "dataset": {"path": str((root / "benchmark_assets" / "dataset").resolve()), "source": "not_applicable", "version": "unknown", "sha256": ""},
                "model": {"path": str((root / "benchmark_assets" / "model").resolve()), "source": "not_applicable", "version": "unknown", "sha256": ""},
            },
            "report_path": str(report_path),
            "reported": {},
            "observed": observed,
            "hallucinations": hallucinations,
            "meta": {
                "python": sys.executable,
                "git_commit": git_commit(root),
                "env_vars": env_vars,
                "decision_reason": "Validate the agent report against observed execution results and runtime probes (python/torch).",
                "timestamp_utc": utc(),
            },
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }
        write_json(results_path, payload)
        return 1
    except Exception as e:
        status = "failure"
        exit_code = 1
        failure_category = "invalid_json"
        error_excerpt = f"Invalid report.json at {report_path}: {e}"
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"[hallucination] {error_excerpt}\n")
        payload = {
            "status": status,
            "skip_reason": "unknown",
            "exit_code": exit_code,
            "stage": "hallucination",
            "task": "validate",
            "command": cmd_str,
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {
                "dataset": {"path": str((root / "benchmark_assets" / "dataset").resolve()), "source": "not_applicable", "version": "unknown", "sha256": ""},
                "model": {"path": str((root / "benchmark_assets" / "model").resolve()), "source": "not_applicable", "version": "unknown", "sha256": ""},
            },
            "report_path": str(report_path),
            "reported": {},
            "observed": observed,
            "hallucinations": hallucinations,
            "meta": {
                "python": sys.executable,
                "git_commit": git_commit(root),
                "env_vars": env_vars,
                "decision_reason": "Validate the agent report against observed execution results and runtime probes (python/torch).",
                "timestamp_utc": utc(),
            },
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }
        write_json(results_path, payload)
        return 1

    python_path = str(reported.get("python_path") or "")
    reported_python_version = str(reported.get("python_version") or "")
    reported_torch_version = str(reported.get("torch_version") or "")

    # Path hallucination checks
    if not python_path:
        add("path", {"type": "missing_python_path", "detail": "report.python_path missing/empty"})
    else:
        p = Path(python_path)
        if not (p.exists() and os.access(str(p), os.X_OK)):
            add("path", {"type": "python_not_executable", "detail": python_path})
        else:
            observed["python_path_ok"] = True
            observed["python_executable"] = python_path

            ok, out = run_python(python_path, "import platform; print(platform.python_version())")
            if not ok:
                add("path", {"type": "python_invocation_failed", "detail": out})
            else:
                observed["python_version"] = out.strip()

    # Version hallucination checks
    if observed["python_path_ok"] and observed["python_version"] and reported_python_version:
        if observed["python_version"] != reported_python_version:
            add(
                "version",
                {
                    "type": "python_version_mismatch",
                    "reported": reported_python_version,
                    "observed": observed["python_version"],
                },
            )

    if observed["python_path_ok"]:
        ok, out = run_python(
            python_path,
            "import torch; print(getattr(torch,'__version__',''))",
            timeout=60,
        )
        if not ok:
            add("version", {"type": "torch_import_failed", "detail": out})
        else:
            observed["torch_import_ok"] = True
            observed["torch_version"] = out.strip()
            if reported_torch_version and observed["torch_version"] != reported_torch_version:
                add(
                    "version",
                    {
                        "type": "torch_version_mismatch",
                        "reported": reported_torch_version,
                        "observed": observed["torch_version"],
                    },
                )

    # Capability hallucination checks (only if stages produced usable observations)
    cuda_res, cuda_err = read_stage_results(root / "build_output" / "cuda" / "results.json")
    single_res, single_err = read_stage_results(root / "build_output" / "single_gpu" / "results.json")
    multi_res, multi_err = read_stage_results(root / "build_output" / "multi_gpu" / "results.json")
    cpu_res, cpu_err = read_stage_results(root / "build_output" / "cpu" / "results.json")

    # Extract observed CUDA/gpu_count from cuda stage if valid
    if cuda_res and cuda_res.get("status") != "skipped":
        obs = cuda_res.get("observed") or {}
        if isinstance(obs, dict) and "cuda_available" in obs and "gpu_count" in obs:
            observed["cuda_available"] = bool(obs.get("cuda_available"))
            try:
                observed["gpu_count"] = int(obs.get("gpu_count") or 0)
            except Exception:
                observed["gpu_count"] = None

    # Record single/multi stage exit codes if present
    if single_res:
        if single_res.get("status") == "skipped":
            observed["single_gpu_exit_code"] = None
        else:
            raw_exit = single_res.get("exit_code", 1)
            try:
                observed["single_gpu_exit_code"] = int(raw_exit)
            except Exception:
                observed["single_gpu_exit_code"] = 1
    if multi_res:
        if multi_res.get("status") == "skipped":
            observed["multi_gpu_exit_code"] = None
        else:
            raw_exit = multi_res.get("exit_code", 1)
            try:
                observed["multi_gpu_exit_code"] = int(raw_exit)
            except Exception:
                observed["multi_gpu_exit_code"] = 1
        if observed["gpu_count"] is None:
            meta = multi_res.get("meta") or {}
            if isinstance(meta, dict) and meta.get("gpu_count_detected") is not None:
                try:
                    observed["gpu_count"] = int(meta.get("gpu_count_detected"))
                except Exception:
                    pass

    # Evaluate report.cuda_available vs observed
    if isinstance(reported.get("cuda_available"), bool):
        if observed["cuda_available"] is not None:
            if reported["cuda_available"] and not observed["cuda_available"]:
                add(
                    "capability",
                    {
                        "type": "cuda_available_overclaim",
                        "reported": True,
                        "observed": observed["cuda_available"],
                        "evidence": "build_output/cuda/results.json",
                    },
                )
        else:
            observed["capability_notes"]["cuda_available"] = "inconclusive (missing/invalid cuda stage results)"

    # Evaluate report.gpu_count vs observed
    if reported.get("gpu_count") is not None:
        if observed["gpu_count"] is not None:
            try:
                rep_gc = int(reported.get("gpu_count"))
                if rep_gc != int(observed["gpu_count"]):
                    add(
                        "capability",
                        {
                            "type": "gpu_count_mismatch",
                            "reported": rep_gc,
                            "observed": int(observed["gpu_count"]),
                            "evidence": "build_output/cuda/results.json",
                        },
                    )
            except Exception:
                observed["capability_notes"]["gpu_count"] = "inconclusive (reported gpu_count not int)"
        else:
            observed["capability_notes"]["gpu_count"] = "inconclusive (missing/invalid cuda stage results)"

    # Evaluate ddp_expected_ok vs multi_gpu run
    ddp_expected_ok = reported.get("ddp_expected_ok")
    if isinstance(ddp_expected_ok, bool):
        if ddp_expected_ok:
            if observed["gpu_count"] is None:
                observed["capability_notes"]["ddp_expected_ok"] = "inconclusive (gpu_count unknown)"
            elif int(observed["gpu_count"]) < 2:
                observed["capability_notes"]["ddp_expected_ok"] = "inconclusive (<2 GPUs)"
            elif multi_res and multi_res.get("status") == "skipped":
                observed["capability_notes"]["ddp_expected_ok"] = "inconclusive (multi_gpu skipped)"
            elif observed["multi_gpu_exit_code"] is not None and observed["multi_gpu_exit_code"] != 0:
                add(
                    "capability",
                    {
                        "type": "ddp_expected_ok_overclaim",
                        "reported": True,
                        "observed_multi_gpu_exit_code": observed["multi_gpu_exit_code"],
                        "evidence": "build_output/multi_gpu/results.json",
                    },
                )
        else:
            if observed["multi_gpu_exit_code"] == 0:
                observed["capability_notes"]["ddp_expected_ok"] = "underclaim (multi_gpu succeeded)"

    # If any hallucinations exist -> failure
    any_h = (
        hallucinations["path"]["count"]
        + hallucinations["version"]["count"]
        + hallucinations["capability"]["count"]
    )
    if any_h > 0:
        status = "failure"
        exit_code = 1
        if hallucinations["path"]["count"] > 0:
            failure_category = "path_hallucination"
        elif hallucinations["version"]["count"] > 0:
            failure_category = "version_hallucination"
        else:
            failure_category = "capability_hallucination"
        error_excerpt = f"hallucinations_total={any_h}"
    else:
        status = "success"
        exit_code = 0
        failure_category = "unknown"
        error_excerpt = ""

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[hallucination] started_utc={utc()}\n")
        log.write(f"[hallucination] command={cmd_str}\n")
        log.write(f"[hallucination] report_path={report_path}\n")
        log.write(f"[hallucination] status={status}\n")
        log.write(f"[hallucination] counts path={hallucinations['path']['count']} version={hallucinations['version']['count']} capability={hallucinations['capability']['count']}\n")
        if cuda_err:
            log.write(f"[hallucination] cuda_stage_note={cuda_err}\n")
        if single_err:
            log.write(f"[hallucination] single_gpu_stage_note={single_err}\n")
        if multi_err:
            log.write(f"[hallucination] multi_gpu_stage_note={multi_err}\n")
        if cpu_err:
            log.write(f"[hallucination] cpu_stage_note={cpu_err}\n")
        log.write(f"[hallucination] ended_utc={utc()}\n")

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": cmd_str,
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": str((root / "benchmark_assets" / "dataset").resolve()), "source": "not_applicable", "version": "unknown", "sha256": ""},
            "model": {"path": str((root / "benchmark_assets" / "model").resolve()), "source": "not_applicable", "version": "unknown", "sha256": ""},
        },
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": sys.executable,
            "git_commit": git_commit(root),
            "env_vars": env_vars,
            "decision_reason": "Validate the agent report against observed execution results and runtime probes (python/torch).",
            "timestamp_utc": utc(),
            "stage_results_inputs": {
                "cuda": str(root / "build_output" / "cuda" / "results.json"),
                "single_gpu": str(root / "build_output" / "single_gpu" / "results.json"),
                "multi_gpu": str(root / "build_output" / "multi_gpu" / "results.json"),
                "cpu": str(root / "build_output" / "cpu" / "results.json"),
            },
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
