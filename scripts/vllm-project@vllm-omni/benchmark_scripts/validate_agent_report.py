#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: pathlib.Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tail_text(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)


def resolve_report_path(cli_path: str | None) -> pathlib.Path:
    if cli_path:
        return pathlib.Path(cli_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return pathlib.Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return pathlib.Path(DEFAULT_REPORT_PATH)


def load_json(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_python(python_path: str, code: str, timeout_sec: int = 30) -> tuple[int, str, str]:
    proc = subprocess.run([python_path, "-c", code], capture_output=True, text=True, timeout=timeout_sec)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def stage_result(repo_root: pathlib.Path, stage: str) -> dict[str, Any] | None:
    path = repo_root / "build_output" / stage / "results.json"
    if not path.exists():
        return None
    return load_json(path)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Validate agent report and compute hallucination statistics.")
    ap.add_argument("--report-path", default=None, help="Override report.json path.")
    args = ap.parse_args(argv)

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    out_dir = repo_root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)

    hallucinations: dict[str, dict[str, Any]] = {
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
        "cpu_exit_code": None,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
        "cpu_status": None,
        "single_gpu_status": None,
        "multi_gpu_status": None,
        "cuda_stage_exit_code": None,
        "cuda_stage_status": None,
    }

    reported: dict[str, Any] = {}
    status = "failure"
    exit_code = 1
    failure_category = "unknown"

    with log_path.open("w", encoding="utf-8") as log_fp:
        log_fp.write(f"[hallucination] time_utc={now_utc_iso()}\n")
        log_fp.write(f"[hallucination] report_path={report_path}\n")

        report_data: dict[str, Any] | None = None
        try:
            report_data = json.loads(report_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            failure_category = "missing_report"
            log_fp.write("[hallucination] report missing\n")
        except Exception as e:
            failure_category = "invalid_json"
            log_fp.write(f"[hallucination] report parse failed: {type(e).__name__}: {e}\n")

        if not isinstance(report_data, dict):
            result = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "hallucination",
                "task": "validate",
                "command": f"{pathlib.Path(__file__).name} --report-path {report_path}",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "report_path": str(report_path),
                "reported": {},
                "observed": observed,
                "hallucinations": hallucinations,
                "meta": {"timestamp_utc": now_utc_iso()},
                "failure_category": failure_category,
                "error_excerpt": tail_text(log_path),
            }
            write_json(results_path, result)
            return 1

        reported = dict(report_data)

        python_path = reported.get("python_path")
        if not isinstance(python_path, str) or not python_path:
            hallucinations["path"]["items"].append({"type": "missing_python_path", "message": "python_path missing"})
        else:
            observed["python_executable"] = python_path
            if not (os.path.isfile(python_path) and os.access(python_path, os.X_OK)):
                hallucinations["path"]["items"].append(
                    {"type": "python_path_not_executable", "message": f"python_path not executable: {python_path!r}"}
                )
            else:
                observed["python_path_ok"] = True
                rc, out, err = run_python(
                    python_path, 'import platform; print(platform.python_version())', timeout_sec=20
                )
                if rc != 0:
                    hallucinations["path"]["items"].append(
                        {"type": "python_path_exec_failed", "message": f"python_path failed to run: {err or out}"}
                    )
                else:
                    observed["python_version"] = out

        # Version hallucinations (only if python_path is executable).
        if observed["python_path_ok"]:
            reported_py_ver = reported.get("python_version")
            if isinstance(reported_py_ver, str) and reported_py_ver:
                if observed["python_version"] and reported_py_ver != observed["python_version"]:
                    hallucinations["version"]["items"].append(
                        {
                            "type": "python_version_mismatch",
                            "reported": reported_py_ver,
                            "observed": observed["python_version"],
                        }
                    )

            reported_torch_ver = reported.get("torch_version")
            if isinstance(reported_torch_ver, str) and reported_torch_ver:
                rc, out, err = run_python(python_path, "import torch; print(torch.__version__)", timeout_sec=30)
                if rc != 0:
                    hallucinations["version"]["items"].append(
                        {"type": "torch_import_failed", "reported": reported_torch_ver, "error": err or out}
                    )
                else:
                    observed["torch_import_ok"] = True
                    observed["torch_version"] = out
                    if out and out != reported_torch_ver:
                        hallucinations["version"]["items"].append(
                            {"type": "torch_version_mismatch", "reported": reported_torch_ver, "observed": out}
                        )

        # Load stage evidence.
        cuda_res = stage_result(repo_root, "cuda")
        single_res = stage_result(repo_root, "single_gpu")
        multi_res = stage_result(repo_root, "multi_gpu")
        cpu_res = stage_result(repo_root, "cpu")

        def pick_status_exit(res: dict[str, Any] | None) -> tuple[str | None, int | None]:
            if not isinstance(res, dict):
                return None, None
            st = res.get("status")
            ec = res.get("exit_code")
            return (st if isinstance(st, str) else None, int(ec) if isinstance(ec, int) else None)

        observed["cuda_stage_status"], observed["cuda_stage_exit_code"] = pick_status_exit(cuda_res)
        observed["single_gpu_status"], observed["single_gpu_exit_code"] = pick_status_exit(single_res)
        observed["multi_gpu_status"], observed["multi_gpu_exit_code"] = pick_status_exit(multi_res)
        observed["cpu_status"], observed["cpu_exit_code"] = pick_status_exit(cpu_res)

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

        # Capability hallucinations (only when observations are usable).
        # 1) report.cuda_available == true but CUDA check failed
        rep_cuda_avail = reported.get("cuda_available")
        if isinstance(rep_cuda_avail, bool):
            if rep_cuda_avail is True and observed["cuda_stage_exit_code"] == 1:
                hallucinations["capability"]["items"].append(
                    {
                        "type": "cuda_available_mismatch",
                        "reported": True,
                        "observed_exit_code": observed["cuda_stage_exit_code"],
                        "note": "CUDA stage failed but report claimed cuda_available=true",
                    }
                )

        # 2) gpu_count mismatch (only if we have an observed gpu_count)
        rep_gpu_count = reported.get("gpu_count")
        if isinstance(rep_gpu_count, int) and observed["gpu_count"] is not None:
            if rep_gpu_count != observed["gpu_count"]:
                hallucinations["capability"]["items"].append(
                    {"type": "gpu_count_mismatch", "reported": rep_gpu_count, "observed": observed["gpu_count"]}
                )

        # 3) ddp_expected_ok == true and >=2 GPUs and multi-gpu run failed (unless skipped)
        ddp_expected_ok = reported.get("ddp_expected_ok")
        multi_status = observed["multi_gpu_status"]
        if isinstance(ddp_expected_ok, bool) and ddp_expected_ok is True:
            if multi_status == "skipped":
                # Inconclusive: missing repo functionality shouldn't count as hallucination.
                pass
            else:
                multi_failure_category = ""
                if isinstance(multi_res, dict) and isinstance(multi_res.get("failure_category"), str):
                    multi_failure_category = multi_res.get("failure_category") or ""

                # If the benchmark harness couldn't actually execute the repo entrypoint,
                # we treat this as inconclusive (not a capability hallucination).
                harness_failure_categories = {
                    "entrypoint_not_found",
                    "args_unknown",
                    "missing_report",
                    "invalid_json",
                    "deps",
                    "download_failed",
                    "data",
                    "model",
                }
                if multi_failure_category in harness_failure_categories:
                    pass
                else:
                    # Need GPU count to decide conclusiveness.
                    if isinstance(observed["gpu_count"], int) and observed["gpu_count"] < 2:
                        # Inconclusive due to hardware.
                        pass
                    elif isinstance(observed["gpu_count"], int) and observed["gpu_count"] >= 2:
                        if observed["multi_gpu_exit_code"] == 1:
                            hallucinations["capability"]["items"].append(
                                {
                                    "type": "ddp_expected_ok_but_multi_failed",
                                    "reported": True,
                                    "observed_multi_gpu_exit_code": observed["multi_gpu_exit_code"],
                                }
                            )

        # Finalize counts and status.
        for k in ("path", "version", "capability"):
            items = hallucinations[k]["items"]
            hallucinations[k]["count"] = len(items)

        total_h = hallucinations["path"]["count"] + hallucinations["version"]["count"] + hallucinations["capability"][
            "count"
        ]

        if hallucinations["path"]["count"] > 0:
            failure_category = "path_hallucination"
        elif hallucinations["version"]["count"] > 0:
            failure_category = "version_hallucination"
        elif hallucinations["capability"]["count"] > 0:
            failure_category = "capability_hallucination"
        else:
            failure_category = ""

        status = "success" if total_h == 0 else "failure"
        exit_code = 0 if total_h == 0 else 1

    result = {
        "status": status,
        "skip_reason": "not_applicable" if status == "success" else "unknown",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"{pathlib.Path(__file__).name} --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {"timestamp_utc": now_utc_iso()},
        "failure_category": failure_category,
        "error_excerpt": tail_text(log_path),
    }
    write_json(results_path, result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
