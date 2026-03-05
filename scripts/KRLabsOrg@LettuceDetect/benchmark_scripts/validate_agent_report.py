#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path("/opt/scimlopsbench/report.json")


def _git_commit(repo: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            check=False,
            capture_output=True,
            text=True,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def _tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    return "\n".join(lines[-max_lines:])


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def _load_stage_results(repo: Path, stage: str) -> tuple[Optional[dict[str, Any]], str]:
    path = repo / "build_output" / stage / "results.json"
    if not path.exists():
        return None, f"missing {path}"
    try:
        data = _read_json(path)
        if not isinstance(data, dict):
            return None, f"invalid JSON type in {path}"
        return data, ""
    except Exception as e:
        return None, f"invalid JSON in {path}: {e!r}"


def _stage_outcome(stage_results: Optional[dict[str, Any]]) -> str:
    if not stage_results:
        return "missing"
    status = str(stage_results.get("status", "failure"))
    exit_code = int(stage_results.get("exit_code", 1))
    if status == "skipped":
        return "skipped"
    if status == "success" and exit_code == 0:
        return "success"
    return "failure"


def _run_python_probe(python_path: str, code: str, timeout_sec: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [python_path, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate agent report.json and compute hallucination stats")
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    repo = _repo_root()
    out_dir = repo / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)
    cmd_display = "python benchmark_scripts/validate_agent_report.py" + (
        f" --report-path {report_path}" if args.report_path else ""
    )

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as logf:
            logf.write(msg.rstrip() + "\n")

    # Start log
    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"timestamp_utc={_utc_timestamp()}\n")
        logf.write(f"report_path={report_path}\n")
        logf.write(f"sys.executable={sys.executable}\n")

    hallucinations: dict[str, dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }
    inconclusive: list[dict[str, Any]] = []

    reported: dict[str, Any] = {}
    observed: dict[str, Any] = {
        "python_path_ok": False,
        "python_executable": "",
        "python_version": "",
        "torch_import_ok": False,
        "torch_version": "",
        "cuda_available": False,
        "gpu_count": 0,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
    }

    status = "failure"
    exit_code = 1
    failure_category = "unknown"

    # Load report
    if not report_path.exists():
        failure_category = "missing_report"
        log(f"ERROR: report missing: {report_path}")
    else:
        try:
            report = _read_json(report_path)
            if not isinstance(report, dict):
                raise ValueError("report.json is not a JSON object")
            reported = report
        except Exception as e:
            failure_category = "invalid_json"
            log(f"ERROR: invalid report JSON: {e!r}")
            report = None  # type: ignore[assignment]

    python_path = reported.get("python_path") if isinstance(reported, dict) else None
    if not isinstance(python_path, str) or not python_path.strip():
        hallucinations["path"]["items"].append(
            {"type": "python_path_missing", "message": "python_path missing in report.json"}
        )
    else:
        python_path = python_path.strip()
        observed["python_executable"] = python_path
        if not _is_executable_file(Path(python_path)):
            hallucinations["path"]["items"].append(
                {
                    "type": "python_path_not_executable",
                    "message": f"python_path is not an executable file: {python_path!r}",
                }
            )
        else:
            # python_path -c probe for python version (path hallucination if fails)
            try:
                cp = _run_python_probe(
                    python_path,
                    "import platform; print(platform.python_version())",
                    timeout_sec=30,
                )
                if cp.returncode != 0:
                    hallucinations["path"]["items"].append(
                        {
                            "type": "python_probe_failed",
                            "message": f"python_path probe failed rc={cp.returncode}",
                            "stderr_tail": (cp.stderr or "").splitlines()[-20:],
                        }
                    )
                else:
                    observed["python_path_ok"] = True
                    observed["python_version"] = (cp.stdout or "").strip()
            except Exception as e:
                hallucinations["path"]["items"].append(
                    {
                        "type": "python_probe_exception",
                        "message": f"python_path probe exception: {e!r}",
                    }
                )

    # Version hallucinations
    if observed["python_path_ok"]:
        reported_pyver = reported.get("python_version")
        if isinstance(reported_pyver, str) and reported_pyver.strip():
            if observed["python_version"] and observed["python_version"] != reported_pyver.strip():
                hallucinations["version"]["items"].append(
                    {
                        "type": "python_version_mismatch",
                        "reported": reported_pyver.strip(),
                        "observed": observed["python_version"],
                    }
                )
        else:
            log("NOTE: report.python_version missing; version check skipped")

        # Torch version probe
        try:
            cp = _run_python_probe(
                observed["python_executable"],
                r"""
import json
obs = {}
try:
  import torch
  obs["torch_import_ok"] = True
  obs["torch_version"] = getattr(torch, "__version__", "")
  obs["cuda_available"] = bool(torch.cuda.is_available())
  obs["gpu_count"] = int(torch.cuda.device_count()) if obs["cuda_available"] else 0
except Exception as e:
  obs["torch_import_ok"] = False
  obs["torch_error"] = repr(e)
print(json.dumps(obs))
""",
                timeout_sec=60,
            )
            if cp.returncode == 0:
                torch_obs = json.loads((cp.stdout or "").strip() or "{}")
                if isinstance(torch_obs, dict):
                    observed["torch_import_ok"] = bool(torch_obs.get("torch_import_ok", False))
                    observed["torch_version"] = str(torch_obs.get("torch_version", "") or "")
                    observed["cuda_available"] = bool(torch_obs.get("cuda_available", False))
                    observed["gpu_count"] = int(torch_obs.get("gpu_count", 0) or 0)
                    if not observed["torch_import_ok"]:
                        hallucinations["version"]["items"].append(
                            {
                                "type": "torch_import_failed",
                                "message": str(torch_obs.get("torch_error", "import torch failed")),
                            }
                        )
                    else:
                        reported_torch = reported.get("torch_version")
                        if isinstance(reported_torch, str) and reported_torch.strip():
                            if observed["torch_version"] != reported_torch.strip():
                                hallucinations["version"]["items"].append(
                                    {
                                        "type": "torch_version_mismatch",
                                        "reported": reported_torch.strip(),
                                        "observed": observed["torch_version"],
                                    }
                                )
                        else:
                            log("NOTE: report.torch_version missing; version check skipped")
            else:
                hallucinations["version"]["items"].append(
                    {
                        "type": "torch_probe_failed",
                        "message": f"torch probe rc={cp.returncode}",
                        "stderr_tail": (cp.stderr or "").splitlines()[-20:],
                    }
                )
        except Exception as e:
            hallucinations["version"]["items"].append(
                {"type": "torch_probe_exception", "message": f"{e!r}"}
            )

    # Capability hallucinations (based on stage results when available)
    cuda_stage, cuda_stage_err = _load_stage_results(repo, "cuda")
    single_stage, single_stage_err = _load_stage_results(repo, "single_gpu")
    multi_stage, multi_stage_err = _load_stage_results(repo, "multi_gpu")
    cpu_stage, _ = _load_stage_results(repo, "cpu")

    observed["single_gpu_exit_code"] = (
        int(single_stage.get("exit_code")) if isinstance(single_stage, dict) and "exit_code" in single_stage else None
    )
    observed["multi_gpu_exit_code"] = (
        int(multi_stage.get("exit_code")) if isinstance(multi_stage, dict) and "exit_code" in multi_stage else None
    )

    # Use CUDA stage as canonical observed cuda/gpu_count when it contains a valid measurement.
    cuda_outcome = _stage_outcome(cuda_stage)
    if cuda_outcome in {"missing"}:
        inconclusive.append({"capability": "cuda", "assessment": "inconclusive", "reason": cuda_stage_err})
    elif cuda_outcome == "skipped":
        inconclusive.append({"capability": "cuda", "assessment": "inconclusive", "reason": "stage_skipped"})
    else:
        cuda_failure_category = (
            str(cuda_stage.get("failure_category", "")) if isinstance(cuda_stage, dict) else ""
        )
        cuda_obs = (cuda_stage.get("observed", {}) if isinstance(cuda_stage, dict) else {}) or {}
        has_cuda_measurement = isinstance(cuda_obs, dict) and isinstance(cuda_obs.get("cuda_available"), bool) and isinstance(
            cuda_obs.get("gpu_count"), int
        )

        if cuda_failure_category in {"missing_report", "invalid_json"} or not has_cuda_measurement:
            inconclusive.append(
                {
                    "capability": "cuda",
                    "assessment": "inconclusive",
                    "reason": "cuda stage has no valid observation (missing_report/invalid_json/no measurement)",
                }
            )
        else:
            cuda_ok = cuda_outcome == "success"
            reported_cuda = reported.get("cuda_available")
            if isinstance(reported_cuda, bool) and reported_cuda and not cuda_ok:
                hallucinations["capability"]["items"].append(
                    {
                        "type": "cuda_available_mismatch",
                        "reported": True,
                        "observed": False,
                        "evidence": "build_output/cuda/results.json",
                    }
                )

            measured_gpu_count: Optional[int] = int(cuda_obs.get("gpu_count", 0))
            reported_gpu_count = reported.get("gpu_count")
            if isinstance(reported_gpu_count, int):
                if int(reported_gpu_count) != int(measured_gpu_count):
                    hallucinations["capability"]["items"].append(
                        {
                            "type": "gpu_count_mismatch",
                            "reported": int(reported_gpu_count),
                            "observed": int(measured_gpu_count),
                            "evidence": "build_output/cuda/results.json",
                        }
                    )
            else:
                inconclusive.append(
                    {
                        "capability": "gpu_count",
                        "assessment": "inconclusive",
                        "reason": "missing reported gpu_count in report.json",
                    }
                )

    # DDP expected OK check
    ddp_expected_ok = reported.get("ddp_expected_ok")
    if isinstance(ddp_expected_ok, bool) and ddp_expected_ok:
        measured_gpu_count = None
        if isinstance(cuda_stage, dict):
            if str(cuda_stage.get("failure_category", "")) not in {"missing_report", "invalid_json"}:
                obs = cuda_stage.get("observed", {})
                if isinstance(obs, dict) and isinstance(obs.get("gpu_count"), int):
                    measured_gpu_count = int(obs["gpu_count"])

        if measured_gpu_count is None:
            inconclusive.append(
                {"capability": "ddp_expected_ok", "assessment": "inconclusive", "reason": "gpu_count unknown"}
            )
        elif measured_gpu_count < 2:
            inconclusive.append(
                {
                    "capability": "ddp_expected_ok",
                    "assessment": "inconclusive",
                    "reason": f"<2 GPUs available (gpu_count={measured_gpu_count})",
                }
            )
        else:
            multi_outcome = _stage_outcome(multi_stage)
            if multi_outcome == "skipped":
                inconclusive.append(
                    {"capability": "ddp_expected_ok", "assessment": "inconclusive", "reason": "multi_gpu skipped"}
                )
            elif multi_outcome == "missing":
                inconclusive.append(
                    {"capability": "ddp_expected_ok", "assessment": "inconclusive", "reason": multi_stage_err}
                )
            elif multi_outcome != "success":
                hallucinations["capability"]["items"].append(
                    {
                        "type": "ddp_expected_ok_but_multi_gpu_failed",
                        "reported": True,
                        "observed": False,
                        "evidence": "build_output/multi_gpu/results.json",
                    }
                )

    # Count hallucinations
    for k in ("path", "version", "capability"):
        hallucinations[k]["count"] = len(hallucinations[k]["items"])

    any_hallucination = any(hallucinations[k]["count"] > 0 for k in hallucinations)

    if failure_category in {"missing_report", "invalid_json"}:
        status = "failure"
        exit_code = 1
    elif any_hallucination:
        status = "failure"
        exit_code = 1
        if hallucinations["path"]["count"] > 0:
            failure_category = "path_hallucination"
        elif hallucinations["version"]["count"] > 0:
            failure_category = "version_hallucination"
        else:
            failure_category = "capability_hallucination"
    else:
        status = "success"
        exit_code = 0
        failure_category = "unknown"

    payload: dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": cmd_display,
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
            "model": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        },
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "inconclusive": inconclusive,
        "hallucinations": hallucinations,
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo),
            "timestamp_utc": _utc_timestamp(),
            "env_vars": {
                k: ("***REDACTED***" if k in {"HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "OPENAI_API_KEY"} else v)
                for k, v in os.environ.items()
                if k
                in {
                    "CUDA_VISIBLE_DEVICES",
                    "SCIMLOPSBENCH_REPORT",
                    "SCIMLOPSBENCH_PYTHON",
                    "HF_TOKEN",
                    "HUGGINGFACE_HUB_TOKEN",
                    "OPENAI_API_KEY",
                }
            },
            "decision_reason": "Validate agent self-report (report.json) against executable probes and benchmark stage results to detect path/version/capability hallucinations.",
            "stage_results_sources": {
                "cuda": "build_output/cuda/results.json",
                "single_gpu": "build_output/single_gpu/results.json",
                "multi_gpu": "build_output/multi_gpu/results.json",
                "cpu": "build_output/cpu/results.json",
            },
        },
        "failure_category": failure_category,
        "error_excerpt": _tail_text(log_path, max_lines=220),
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log("\n--- results ---")
    log(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
