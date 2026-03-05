#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_last_lines(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= max_lines:
                step = block if size >= block else size
                size -= step
                f.seek(size, os.SEEK_SET)
                data = f.read(step) + data
            lines = data.splitlines()[-max_lines:]
            return "\n".join(l.decode("utf-8", errors="replace") for l in lines)
    except Exception:
        return ""


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT", "")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def _base_assets() -> dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _git_commit(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""


def _safe_load_stage_results(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, "missing"
    try:
        return _read_json(path), ""
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception as e:
        return None, f"error:{e!r}"


def _run_python_probe(python_exe: str, code: str, timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [python_exe, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return int(proc.returncode), proc.stdout, proc.stderr
    except Exception as e:
        return 1, "", repr(e)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate agent report.json and compute hallucination stats.")
    parser.add_argument("--report-path", default=None)
    ns = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "hallucination"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stage = "hallucination"
    task = "validate"
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"

    hallucinations: dict[str, Any] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    observed: dict[str, Any] = {
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

    status = "failure"
    exit_code = 1
    failure_category = "unknown"
    report_path = _resolve_report_path(ns.report_path)

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[hallucination] report_path={report_path}\n")

        # Load report
        try:
            report = _read_json(report_path)
        except FileNotFoundError:
            report = None
            failure_category = "missing_report"
            log_f.write("[hallucination] missing report.json\n")
        except json.JSONDecodeError as e:
            report = None
            failure_category = "invalid_json"
            log_f.write(f"[hallucination] invalid report.json: {e}\n")

        reported: dict[str, Any] = report if isinstance(report, dict) else {}

        python_path = str(reported.get("python_path") or "")
        if not python_path:
            hallucinations["path"]["items"].append({"field": "python_path", "issue": "missing"})
        else:
            p = Path(python_path)
            if not (p.exists() and os.access(str(p), os.X_OK)):
                hallucinations["path"]["items"].append(
                    {"field": "python_path", "issue": "not_executable", "value": python_path}
                )
            else:
                observed["python_path_ok"] = True
                observed["python_executable"] = python_path
                rc, out, err = _run_python_probe(
                    python_path, "import platform; print(platform.python_version())", timeout=30
                )
                if rc != 0:
                    observed["python_path_ok"] = False
                    hallucinations["path"]["items"].append(
                        {"field": "python_path", "issue": "invocation_failed", "stderr": err[-1000:]}
                    )
                else:
                    observed["python_version"] = out.strip()

        hallucinations["path"]["count"] = len(hallucinations["path"]["items"])

        # Version checks
        if observed["python_path_ok"]:
            reported_python_version = str(reported.get("python_version") or "")
            if reported_python_version and observed["python_version"] and reported_python_version != observed["python_version"]:
                hallucinations["version"]["items"].append(
                    {
                        "field": "python_version",
                        "reported": reported_python_version,
                        "observed": observed["python_version"],
                    }
                )

            reported_torch_version = str(reported.get("torch_version") or "")
            rc, out, err = _run_python_probe(
                observed["python_executable"],
                "import torch; print(getattr(torch,'__version__',''))",
                timeout=60,
            )
            if rc == 0:
                observed["torch_import_ok"] = True
                observed["torch_version"] = out.strip()
                if reported_torch_version and observed["torch_version"] and reported_torch_version != observed["torch_version"]:
                    hallucinations["version"]["items"].append(
                        {
                            "field": "torch_version",
                            "reported": reported_torch_version,
                            "observed": observed["torch_version"],
                        }
                    )
            else:
                observed["torch_import_ok"] = False
                observed["torch_version"] = ""
                if reported_torch_version:
                    hallucinations["version"]["items"].append(
                        {
                            "field": "torch_version",
                            "reported": reported_torch_version,
                            "observed": "import_failed",
                            "stderr": err[-1000:],
                        }
                    )

        hallucinations["version"]["count"] = len(hallucinations["version"]["items"])

        # Capability checks (only use valid stage results).
        stage_paths = {
            "cuda": repo_root / "build_output" / "cuda" / "results.json",
            "single_gpu": repo_root / "build_output" / "single_gpu" / "results.json",
            "multi_gpu": repo_root / "build_output" / "multi_gpu" / "results.json",
            "cpu": repo_root / "build_output" / "cpu" / "results.json",
        }

        stage_results: dict[str, dict[str, Any] | None] = {}
        stage_errors: dict[str, str] = {}
        for k, p in stage_paths.items():
            data, err = _safe_load_stage_results(p)
            stage_results[k] = data
            stage_errors[k] = err
            if err:
                log_f.write(f"[hallucination] {k} results issue: {p}: {err}\n")

        cuda_res = stage_results.get("cuda") or {}
        cuda_valid = bool(cuda_res) and not stage_errors.get("cuda")
        if cuda_valid:
            obs = cuda_res.get("observed") or {}
            if isinstance(obs, dict):
                observed["cuda_available"] = obs.get("cuda_available")
                observed["gpu_count"] = obs.get("gpu_count")

        def _stage_exit_code(name: str) -> int | None:
            res = stage_results.get(name)
            if not res:
                return None
            try:
                return int(res.get("exit_code"))
            except Exception:
                return None

        observed["single_gpu_exit_code"] = _stage_exit_code("single_gpu")
        observed["multi_gpu_exit_code"] = _stage_exit_code("multi_gpu")
        observed["cpu_exit_code"] = _stage_exit_code("cpu")

        # Rule: report.cuda_available == true but cuda check failed.
        if isinstance(reported, dict) and reported.get("cuda_available") is True:
            if cuda_valid:
                cuda_ok = bool(observed["cuda_available"]) and int(cuda_res.get("exit_code", 1)) == 0
                if not cuda_ok:
                    hallucinations["capability"]["items"].append(
                        {
                            "field": "cuda_available",
                            "reported": True,
                            "observed": observed["cuda_available"],
                            "evidence": "build_output/cuda/results.json",
                        }
                    )
            else:
                # Inconclusive: no valid observation.
                pass

        # Rule: reported gpu_count != measured gpu_count.
        if isinstance(reported, dict) and "gpu_count" in reported:
            if cuda_valid and observed["gpu_count"] is not None:
                try:
                    rep_gc = int(reported.get("gpu_count"))
                    obs_gc = int(observed["gpu_count"])
                    if rep_gc != obs_gc:
                        hallucinations["capability"]["items"].append(
                            {
                                "field": "gpu_count",
                                "reported": rep_gc,
                                "observed": obs_gc,
                                "evidence": "build_output/cuda/results.json",
                            }
                        )
                except Exception:
                    pass

        # Rule: ddp_expected_ok == true and >=2 GPUs and multi-GPU run failed.
        ddp_expected_ok = bool(reported.get("ddp_expected_ok") is True) if isinstance(reported, dict) else False
        if ddp_expected_ok:
            if cuda_valid and isinstance(observed["gpu_count"], int) and int(observed["gpu_count"]) >= 2:
                multi_res = stage_results.get("multi_gpu") or {}
                multi_status = str(multi_res.get("status") or "")
                if multi_status == "skipped":
                    # Inconclusive (skipped by benchmark).
                    pass
                else:
                    multi_exit = observed["multi_gpu_exit_code"]
                    if multi_exit is not None and int(multi_exit) != 0:
                        hallucinations["capability"]["items"].append(
                            {
                                "field": "ddp_expected_ok",
                                "reported": True,
                                "observed": False,
                                "evidence": "build_output/multi_gpu/results.json",
                                "note": ">=2 GPUs observed and multi-gpu stage failed",
                            }
                        )
            else:
                # Inconclusive (<2 GPUs or no valid observation).
                pass

        hallucinations["capability"]["count"] = len(hallucinations["capability"]["items"])

        # Determine overall status / failure category.
        any_hallu = (
            hallucinations["path"]["count"] > 0
            or hallucinations["version"]["count"] > 0
            or hallucinations["capability"]["count"] > 0
        )
        if report is None:
            status = "failure"
            exit_code = 1
            if failure_category not in ("missing_report", "invalid_json"):
                failure_category = "missing_report"
        elif any_hallu:
            status = "failure"
            exit_code = 1
            if hallucinations["path"]["count"] > 0:
                failure_category = "path_hallucination"
            elif hallucinations["version"]["count"] > 0:
                failure_category = "version_hallucination"
            elif hallucinations["capability"]["count"] > 0:
                failure_category = "capability_hallucination"
            else:
                failure_category = "unknown"
        else:
            status = "success"
            exit_code = 0
            failure_category = ""

    results: dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": stage,
        "task": task,
        "command": command,
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": _base_assets(),
        "report_path": str(report_path),
        "reported": reported if isinstance(reported, dict) else {},
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": sys.version.split()[0],
            "git_commit": _git_commit(repo_root),
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            },
            "decision_reason": "Validate agent self-report (report.json) against observed probe/run results and count hallucinations.",
            "timestamp_utc": _now_utc_iso(),
        },
        "failure_category": failure_category,
        "error_excerpt": _read_last_lines(log_path, max_lines=240),
    }
    _write_json(results_path, results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
