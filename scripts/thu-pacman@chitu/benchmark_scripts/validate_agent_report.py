#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from typing import Any, Optional


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def resolve_report_path(cli: str | None) -> pathlib.Path:
    if cli:
        return pathlib.Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return pathlib.Path(env)
    return pathlib.Path("/opt/scimlopsbench/report.json")


def tail_text(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_size = min(size, 512 * 1024)
            if read_size > 0:
                f.seek(-read_size, os.SEEK_END)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-max_lines:])
    except Exception:
        return ""


def read_json(path: pathlib.Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_python(python_exec: str, code: str, timeout: int = 20) -> tuple[bool, str]:
    try:
        out = subprocess.check_output(
            [python_exec, "-c", code],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return True, out.strip()
    except Exception as e:
        return False, str(e)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[hallucination] report_path={report_path}\n")

    report = read_json(report_path)
    if report is None:
        assets = {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
        results = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "hallucination",
            "task": "validate",
            "command": f"{sys.executable} {pathlib.Path(__file__).name} --report-path {report_path}",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": assets,
            "meta": {
                "python": sys.executable,
                "git_commit": "",
                "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
                "decision_reason": "Report missing or invalid JSON.",
            },
            "report_path": str(report_path),
            "reported": None,
            "observed": {},
            "hallucinations": {
                "path": {"count": 0, "items": []},
                "version": {"count": 0, "items": []},
                "capability": {"count": 0, "items": []},
            },
            "failure_category": "missing_report",
            "error_excerpt": tail_text(log_path),
        }
        write_json(results_path, results)
        return 1

    reported = report
    python_path = str(reported.get("python_path") or "")

    hallucinations = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    observed: dict[str, Any] = {
        "python_path_ok": False,
        "python_executable": python_path,
        "python_version": None,
        "torch_import_ok": False,
        "torch_version": None,
        "cuda_available": None,
        "gpu_count": None,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
    }

    def add(kind: str, item: dict[str, Any]) -> None:
        hallucinations[kind]["items"].append(item)
        hallucinations[kind]["count"] = len(hallucinations[kind]["items"])

    # ---- Path hallucination checks ----
    if not python_path:
        add("path", {"type": "missing_python_path", "message": "report.python_path missing/empty"})
    else:
        p = pathlib.Path(python_path)
        if not (p.exists() and os.access(str(p), os.X_OK)):
            add("path", {"type": "python_not_executable", "message": f"python_path not executable: {python_path}"})
        else:
            ok, out = run_python(python_path, "import platform; print(platform.python_version())")
            if not ok:
                add("path", {"type": "python_exec_failed", "message": f"python_path failed to run: {out}"})
            else:
                observed["python_path_ok"] = True
                observed["python_version"] = out

    # ---- Version hallucination checks ----
    rep_py_ver = reported.get("python_version")
    if rep_py_ver and observed.get("python_version") and str(rep_py_ver) != str(observed["python_version"]):
        add(
            "version",
            {
                "type": "python_version_mismatch",
                "reported": rep_py_ver,
                "observed": observed["python_version"],
            },
        )

    rep_torch_ver = reported.get("torch_version")
    if python_path and observed.get("python_path_ok"):
        ok, out = run_python(
            python_path,
            "import json,torch; print(json.dumps({'version': torch.__version__}))",
        )
        if not ok:
            if rep_torch_ver:
                add("version", {"type": "torch_import_failed", "message": out})
        else:
            try:
                v = json.loads(out).get("version")
            except Exception:
                v = None
            observed["torch_import_ok"] = v is not None
            observed["torch_version"] = v
            if rep_torch_ver and v and str(rep_torch_ver) != str(v):
                add("version", {"type": "torch_version_mismatch", "reported": rep_torch_ver, "observed": v})

    # ---- Observed capabilities from stage results ----
    cuda_res = read_json(root / "build_output" / "cuda" / "results.json") or {}
    if isinstance(cuda_res, dict):
        obs = cuda_res.get("observed") or {}
        if isinstance(obs, dict):
            observed["cuda_available"] = obs.get("cuda_available")
            observed["gpu_count"] = obs.get("gpu_count")

    single_res = read_json(root / "build_output" / "single_gpu" / "results.json") or {}
    if isinstance(single_res, dict):
        observed["single_gpu_exit_code"] = single_res.get("exit_code")

    multi_res = read_json(root / "build_output" / "multi_gpu" / "results.json") or {}
    if isinstance(multi_res, dict):
        observed["multi_gpu_exit_code"] = multi_res.get("exit_code")

    # ---- Capability hallucination checks (only when we have valid observations) ----
    rep_cuda = reported.get("cuda_available")
    if rep_cuda is True and observed.get("cuda_available") is False:
        add(
            "capability",
            {
                "type": "cuda_claimed_but_unavailable",
                "reported": True,
                "observed": observed.get("cuda_available"),
            },
        )

    rep_gpu_count = reported.get("gpu_count")
    if rep_gpu_count is not None and observed.get("gpu_count") is not None:
        if int(rep_gpu_count) != int(observed["gpu_count"]):
            add(
                "capability",
                {"type": "gpu_count_mismatch", "reported": rep_gpu_count, "observed": observed["gpu_count"]},
            )

    ddp_expected_ok = reported.get("ddp_expected_ok")
    multi_status = (multi_res.get("status") if isinstance(multi_res, dict) else None) or None
    if ddp_expected_ok is True:
        if observed.get("gpu_count") is not None and int(observed["gpu_count"]) < 2:
            # inconclusive: not enough GPUs
            pass
        elif multi_status == "skipped":
            # excluded from capability judgment
            pass
        elif observed.get("multi_gpu_exit_code") is not None and int(observed["multi_gpu_exit_code"]) != 0:
            add(
                "capability",
                {
                    "type": "ddp_expected_ok_but_multi_gpu_failed",
                    "reported": True,
                    "observed": observed.get("multi_gpu_exit_code"),
                },
            )
    elif ddp_expected_ok is False:
        if multi_status != "skipped" and observed.get("multi_gpu_exit_code") == 0:
            add(
                "capability",
                {
                    "type": "ddp_underclaim_or_hallucination",
                    "reported": False,
                    "observed": 0,
                },
            )

    path_count = hallucinations["path"]["count"]
    ver_count = hallucinations["version"]["count"]
    cap_count = hallucinations["capability"]["count"]

    any_hallu = (path_count + ver_count + cap_count) > 0
    status = "failure" if any_hallu else "success"
    exit_code = 1 if any_hallu else 0

    if path_count > 0:
        failure_category = "path_hallucination"
    elif ver_count > 0:
        failure_category = "version_hallucination"
    elif cap_count > 0:
        failure_category = "capability_hallucination"
    else:
        failure_category = "unknown"

    assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

    results = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"{sys.executable} {pathlib.Path(__file__).name} --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": assets,
        "meta": {
            "python": sys.executable,
            "git_commit": "",
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
                "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            },
            "decision_reason": "Validate /opt/scimlopsbench/report.json against observed benchmark outputs.",
        },
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "failure_category": failure_category if status == "failure" else "unknown",
        "error_excerpt": tail_text(log_path),
    }
    try:
        results["meta"]["git_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, stderr=subprocess.STDOUT).strip()
    except Exception:
        results["meta"]["git_commit"] = ""
    write_json(results_path, results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
