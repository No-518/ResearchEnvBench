#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json: {path}: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"failed reading json: {path}: {e}"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def resolve_report_path(cli_path: Optional[str]) -> str:
    if cli_path:
        return cli_path
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return env_path
    return DEFAULT_REPORT_PATH


def read_stage_results(root: Path, stage: str) -> Tuple[Optional[dict], Optional[str]]:
    return safe_read_json(root / "build_output" / stage / "results.json")


def stage_status_exit(stage_results: Optional[dict]) -> Tuple[Optional[str], Optional[int]]:
    if not isinstance(stage_results, dict):
        return None, None
    status = stage_results.get("status")
    exit_code = stage_results.get("exit_code")
    try:
        exit_code_int = int(exit_code) if exit_code is not None else None
    except Exception:
        exit_code_int = None
    return str(status) if status is not None else None, exit_code_int


def run_python_probe(python_path: str, code: str, timeout: int = 30) -> Tuple[bool, str]:
    try:
        res = subprocess.run(
            [python_path, "-c", code],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if res.returncode != 0:
            return False, (res.stderr.strip() or res.stdout.strip() or f"returncode={res.returncode}")
        return True, res.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, timeout=5)
            .strip()
        )
    except Exception:
        return ""


def load_assets(root: Path) -> Dict[str, Dict[str, str]]:
    manifest = root / "benchmark_assets" / "manifest.json"
    data, _ = safe_read_json(manifest)
    if not isinstance(data, dict):
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    ds = data.get("dataset") if isinstance(data.get("dataset"), dict) else {}
    md = data.get("model") if isinstance(data.get("model"), dict) else {}
    return {
        "dataset": {
            "path": str(ds.get("path", "")),
            "source": str(ds.get("source", "")),
            "version": str(ds.get("version", "")),
            "sha256": str(ds.get("sha256", "")),
        },
        "model": {
            "path": str(md.get("path", "")),
            "source": str(md.get("source", "")),
            "version": str(md.get("version", "")),
            "sha256": str(md.get("sha256", "")),
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate agent report.json and compute hallucination statistics.")
    parser.add_argument("--report-path", default="")
    args = parser.parse_args(argv)

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    log_path.write_text("", encoding="utf-8")

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")

    report_path = resolve_report_path(args.report_path or None)
    log(f"[hallucination] timestamp_utc={utc_ts()}")
    log(f"[hallucination] report_path={report_path}")
    assets = load_assets(root)

    report, report_err = safe_read_json(Path(report_path))
    if report is None or not isinstance(report, dict):
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "hallucination",
            "task": "validate",
            "command": f"{sys.executable} benchmark_scripts/validate_agent_report.py --report-path {report_path}",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {"dataset": assets["dataset"], "model": assets["model"]},
            "report_path": report_path,
            "reported": {},
            "observed": {},
            "hallucinations": {
                "path": {"count": 0, "items": []},
                "version": {"count": 0, "items": []},
                "capability": {"count": 0, "items": []},
            },
            "failure_category": "missing_report" if report_err and report_err.startswith("missing file") else "invalid_json",
            "error_excerpt": report_err or "missing/invalid report",
            "meta": {
                "python": sys.executable,
                "python_version": platform.python_version(),
                "git_commit": git_commit(root),
                "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
                "timestamp_utc": utc_ts(),
            },
        }
        write_json(results_path, payload)
        log(payload["error_excerpt"])
        return 1

    reported = report
    python_path = str(reported.get("python_path", "")).strip()
    reported_python_version = str(reported.get("python_version", "")).strip()
    reported_torch_version = str(reported.get("torch_version", "")).strip()
    reported_cuda_available = bool(reported.get("cuda_available", False))
    reported_gpu_count = reported.get("gpu_count", None)
    reported_ddp_expected_ok = reported.get("ddp_expected_ok", None)

    hallucinations: Dict[str, Dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    observed: Dict[str, Any] = {
        "python_path_ok": False,
        "python_executable": python_path,
        "python_version": "",
        "torch_import_ok": False,
        "torch_version": "",
        "cuda_available": None,
        "gpu_count": None,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
        "cpu_exit_code": None,
        "cuda_stage_exit_code": None,
        "notes": {},
    }

    def add_item(kind: str, item: Dict[str, Any]) -> None:
        hallucinations[kind]["items"].append(item)
        hallucinations[kind]["count"] = len(hallucinations[kind]["items"])

    # ---- Path hallucinations ----
    if not python_path:
        add_item("path", {"type": "python_path_missing", "message": "report.json missing python_path"})
    else:
        py = Path(python_path)
        if not py.exists() or not os.access(str(py), os.X_OK):
            add_item(
                "path",
                {"type": "python_path_not_executable", "message": f"python_path not executable: {python_path}"},
            )
        ok, out = run_python_probe(python_path, "import platform; print(platform.python_version())")
        if not ok:
            add_item("path", {"type": "python_path_exec_failed", "message": out})
        else:
            observed["python_path_ok"] = True
            observed["python_version"] = out

    # ---- Version hallucinations ----
    if observed.get("python_version") and reported_python_version and observed["python_version"] != reported_python_version:
        add_item(
            "version",
            {
                "type": "python_version_mismatch",
                "reported": reported_python_version,
                "observed": observed["python_version"],
            },
        )

    if python_path and Path(python_path).exists() and os.access(python_path, os.X_OK):
        if reported_torch_version:
            ok, out = run_python_probe(python_path, "import torch; print(torch.__version__)")
            if not ok:
                add_item("version", {"type": "torch_import_failed", "message": out})
            else:
                observed["torch_import_ok"] = True
                observed["torch_version"] = out
                if out != reported_torch_version:
                    add_item(
                        "version",
                        {"type": "torch_version_mismatch", "reported": reported_torch_version, "observed": out},
                    )

    # ---- Observed capability evidence (from stage results) ----
    cuda_res, _ = read_stage_results(root, "cuda")
    single_res, _ = read_stage_results(root, "single_gpu")
    multi_res, _ = read_stage_results(root, "multi_gpu")
    cpu_res, _ = read_stage_results(root, "cpu")

    cuda_status, cuda_exit = stage_status_exit(cuda_res)
    single_status, single_exit = stage_status_exit(single_res)
    multi_status, multi_exit = stage_status_exit(multi_res)
    cpu_status, cpu_exit = stage_status_exit(cpu_res)

    observed["cuda_stage_exit_code"] = cuda_exit
    observed["single_gpu_exit_code"] = single_exit
    observed["multi_gpu_exit_code"] = multi_exit
    observed["cpu_exit_code"] = cpu_exit

    # Prefer cuda stage observed fields if present.
    if isinstance(cuda_res, dict):
        obs = cuda_res.get("observed")
        if isinstance(obs, dict):
            if "cuda_available" in obs:
                observed["cuda_available"] = bool(obs.get("cuda_available"))
            if "gpu_count" in obs:
                try:
                    observed["gpu_count"] = int(obs.get("gpu_count"))
                except Exception:
                    pass

    # If cuda stage didn't provide, infer from exit code.
    if observed.get("cuda_available") is None and cuda_exit is not None:
        observed["cuda_available"] = cuda_exit == 0

    # ---- Capability hallucinations ----
    def stage_skipped(s: Optional[str]) -> bool:
        return s == "skipped"

    # (1) CUDA availability claim
    if reported_cuda_available is True:
        if cuda_status is None:
            observed["notes"]["cuda_available"] = "inconclusive_missing_stage_results"
        elif stage_skipped(cuda_status):
            observed["notes"]["cuda_available"] = "inconclusive_skipped"
        else:
            if observed.get("cuda_available") is False or cuda_exit == 1:
                add_item(
                    "capability",
                    {"type": "cuda_available_mismatch", "reported": True, "observed": observed.get("cuda_available")},
                )

    # (2) GPU count claim
    if reported_gpu_count is not None:
        try:
            reported_gpu_count_int = int(reported_gpu_count)
        except Exception:
            reported_gpu_count_int = None
        if reported_gpu_count_int is not None:
            if observed.get("gpu_count") is None:
                observed["notes"]["gpu_count"] = "inconclusive_missing_observation"
            else:
                if int(observed["gpu_count"]) != reported_gpu_count_int:
                    add_item(
                        "capability",
                        {
                            "type": "gpu_count_mismatch",
                            "reported": reported_gpu_count_int,
                            "observed": int(observed["gpu_count"]),
                        },
                    )

    # (3) DDP expected ok claim
    if isinstance(reported_ddp_expected_ok, bool) and reported_ddp_expected_ok is True:
        if observed.get("gpu_count") is None:
            observed["notes"]["ddp_expected_ok"] = "inconclusive_missing_gpu_count"
        elif int(observed["gpu_count"]) < 2:
            observed["notes"]["ddp_expected_ok"] = "inconclusive_insufficient_hardware"
        else:
            if multi_status is None:
                observed["notes"]["ddp_expected_ok"] = "inconclusive_missing_stage_results"
            elif stage_skipped(multi_status):
                observed["notes"]["ddp_expected_ok"] = "inconclusive_skipped"
            else:
                if multi_exit == 1 or multi_status == "failure":
                    add_item(
                        "capability",
                        {
                            "type": "ddp_expected_ok_but_multi_gpu_failed",
                            "reported": True,
                            "observed_multi_gpu_exit_code": multi_exit,
                        },
                    )

    # Final outcome
    any_path = hallucinations["path"]["count"] > 0
    any_version = hallucinations["version"]["count"] > 0
    any_cap = hallucinations["capability"]["count"] > 0

    status = "success"
    exit_code = 0
    failure_category = "unknown"
    if any_path or any_version or any_cap:
        status = "failure"
        exit_code = 1
        if any_path:
            failure_category = "path_hallucination"
        elif any_version:
            failure_category = "version_hallucination"
        else:
            failure_category = "capability_hallucination"

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"{sys.executable} benchmark_scripts/validate_agent_report.py --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {"dataset": assets["dataset"], "model": assets["model"]},
        "report_path": report_path,
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "failure_category": failure_category,
        "error_excerpt": tail_lines(log_path) if status == "failure" else "",
        "meta": {
            "python": sys.executable,
            "python_version": platform.python_version(),
            "git_commit": git_commit(root),
            "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
            "timestamp_utc": utc_ts(),
        },
    }
    write_json(results_path, payload)
    log(f"[hallucination] status={status} exit_code={exit_code} failure_category={failure_category}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
