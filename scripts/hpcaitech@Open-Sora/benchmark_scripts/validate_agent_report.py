#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_report_path(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def tail_text(path: Path, n: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except Exception:
        return ""


def cmd_str(cmd: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def safe_read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except Exception:
        return None, "missing"
    try:
        data = json.loads(raw)
    except Exception:
        return None, "invalid_json"
    if not isinstance(data, dict):
        return None, "invalid_json"
    return data, None


def run_and_capture(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate /opt/scimlopsbench/report.json and compute hallucinations.")
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    log_f = log_path.open("w", encoding="utf-8")

    def log(msg: str) -> None:
        log_f.write(msg + "\n")
        log_f.flush()
        print(msg)

    report_path = resolve_report_path(args.report_path)
    log(f"[hallucination] start_utc={utc_now_iso()}")
    log(f"[hallucination] report_path={report_path}")

    report, rep_err = safe_read_json(report_path)
    if rep_err is not None:
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "hallucination",
            "report_path": str(report_path),
            "reported": {},
            "observed": {},
            "hallucinations": {
                "path": {"count": 0, "items": []},
                "version": {"count": 0, "items": []},
                "capability": {"count": 0, "items": []},
            },
            "failure_category": "missing_report" if rep_err == "missing" else "invalid_json",
            "error_excerpt": tail_text(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log_f.close()
        return 1

    reported = report
    reported_python_path = str(reported.get("python_path") or "").strip()
    reported_python_version = str(reported.get("python_version") or "").strip()
    reported_torch_version = str(reported.get("torch_version") or "").strip()
    reported_cuda_available = reported.get("cuda_available", None)
    reported_gpu_count = reported.get("gpu_count", None)
    reported_ddp_expected_ok = reported.get("ddp_expected_ok", None)

    path_items: list[dict[str, Any]] = []
    version_items: list[dict[str, Any]] = []
    capability_items: list[dict[str, Any]] = []

    python_path_ok = False
    python_executable = reported_python_path
    observed_python_version = ""
    torch_import_ok = False
    observed_torch_version = ""

    # -------------------------
    # Path hallucinations
    # -------------------------
    if not reported_python_path:
        path_items.append({"type": "python_path_missing", "detail": "python_path is missing/empty in report.json"})
    else:
        p = Path(reported_python_path)
        if not p.exists():
            path_items.append({"type": "python_path_not_found", "detail": f"python_path does not exist: {reported_python_path}"})
        elif not os.access(str(p), os.X_OK):
            path_items.append({"type": "python_path_not_executable", "detail": f"python_path not executable: {reported_python_path}"})
        else:
            python_path_ok = True

    if python_path_ok:
        rc, out, err = run_and_capture(
            [reported_python_path, "-c", "import platform; print(platform.python_version())"],
            timeout=15,
        )
        if rc != 0:
            path_items.append(
                {"type": "python_exec_failed", "detail": f"python_path failed to run version probe (rc={rc}): {err.strip()}"}
            )
            python_path_ok = False
        else:
            observed_python_version = out.strip().splitlines()[-1].strip() if out.strip() else ""

    # -------------------------
    # Version hallucinations
    # -------------------------
    if python_path_ok and reported_python_version and observed_python_version and reported_python_version != observed_python_version:
        version_items.append(
            {
                "type": "python_version_mismatch",
                "reported": reported_python_version,
                "observed": observed_python_version,
            }
        )

    if python_path_ok and reported_torch_version:
        rc, out, err = run_and_capture(
            [reported_python_path, "-c", "import torch; print(torch.__version__)"],
            timeout=30,
        )
        if rc != 0:
            version_items.append(
                {
                    "type": "torch_import_failed",
                    "reported": reported_torch_version,
                    "observed": None,
                    "detail": err.strip(),
                }
            )
        else:
            torch_import_ok = True
            observed_torch_version = out.strip().splitlines()[-1].strip() if out.strip() else ""
            if observed_torch_version and observed_torch_version != reported_torch_version:
                version_items.append(
                    {
                        "type": "torch_version_mismatch",
                        "reported": reported_torch_version,
                        "observed": observed_torch_version,
                    }
                )

    # -------------------------
    # Observations from benchmark stages
    # -------------------------
    def load_stage(stage: str) -> tuple[dict[str, Any] | None, str | None]:
        return safe_read_json(root / "build_output" / stage / "results.json")

    cuda_res, cuda_err = load_stage("cuda")
    single_res, single_err = load_stage("single_gpu")
    multi_res, multi_err = load_stage("multi_gpu")
    cpu_res, cpu_err = load_stage("cpu")

    def stage_status(res: dict[str, Any] | None, err: str | None) -> dict[str, Any]:
        if err is not None or res is None:
            return {"available": False, "status": "inconclusive", "exit_code": None, "failure_category": err}
        return {
            "available": True,
            "status": str(res.get("status", "")),
            "exit_code": res.get("exit_code", None),
            "failure_category": str(res.get("failure_category", "")),
        }

    cuda_s = stage_status(cuda_res, cuda_err)
    single_s = stage_status(single_res, single_err)
    multi_s = stage_status(multi_res, multi_err)
    cpu_s = stage_status(cpu_res, cpu_err)

    # CUDA observed (prefer cuda stage observed payload)
    observed_cuda_available = None
    observed_gpu_count = None
    if cuda_res and isinstance(cuda_res.get("observed"), dict):
        o = cuda_res["observed"]
        if "cuda_available" in o:
            observed_cuda_available = bool(o.get("cuda_available"))
        if "gpu_count" in o:
            try:
                observed_gpu_count = int(o.get("gpu_count"))
            except Exception:
                observed_gpu_count = None

    # Fallback GPU count from torch probe (only if torch import ok)
    if observed_gpu_count is None and python_path_ok:
        rc, out, _ = run_and_capture([reported_python_path, "-c", "import torch; print(torch.cuda.device_count())"], timeout=15)
        if rc == 0 and out.strip().isdigit():
            observed_gpu_count = int(out.strip())

    # Capability hallucinations (only when observation is conclusive)
    # 1) report.cuda_available == true but cuda stage indicates unavailable
    if isinstance(reported_cuda_available, bool):
        if cuda_res is None or cuda_err is not None or (cuda_res.get("status") == "skipped"):
            # inconclusive
            pass
        else:
            if reported_cuda_available is True and observed_cuda_available is False:
                capability_items.append(
                    {
                        "type": "cuda_available_overclaim",
                        "reported": True,
                        "observed": observed_cuda_available,
                        "evidence": "build_output/cuda/results.json",
                    }
                )

    # 2) reported gpu_count mismatch
    if isinstance(reported_gpu_count, int) and observed_gpu_count is not None:
        if reported_gpu_count != observed_gpu_count:
            capability_items.append(
                {
                    "type": "gpu_count_mismatch",
                    "reported": reported_gpu_count,
                    "observed": observed_gpu_count,
                    "evidence": "cuda probe / torch.cuda.device_count()",
                }
            )

    # 3) ddp_expected_ok true but multi-gpu run failed (only if >=2 GPUs and stage was actually run)
    if isinstance(reported_ddp_expected_ok, bool) and reported_ddp_expected_ok is True:
        if observed_gpu_count is None or observed_gpu_count < 2:
            # inconclusive (insufficient hardware)
            pass
        else:
            if multi_res is None or multi_err is not None:
                # inconclusive (no observation)
                pass
            elif str(multi_res.get("status")) == "skipped":
                # skipped (repo capability missing) -> inconclusive by spec
                pass
            else:
                multi_failed = (str(multi_res.get("status")) == "failure") or int(multi_res.get("exit_code", 1)) == 1
                if multi_failed:
                    capability_items.append(
                        {
                            "type": "ddp_expected_ok_but_failed",
                            "reported": True,
                            "observed": {"multi_gpu_exit_code": multi_res.get("exit_code")},
                            "evidence": "build_output/multi_gpu/results.json",
                        }
                    )

    observed = {
        "python_path_ok": bool(python_path_ok),
        "python_executable": python_executable,
        "python_version": observed_python_version,
        "torch_import_ok": bool(torch_import_ok),
        "torch_version": observed_torch_version,
        "cuda_available": observed_cuda_available,
        "gpu_count": observed_gpu_count,
        "cpu_exit_code": cpu_s.get("exit_code"),
        "single_gpu_exit_code": single_s.get("exit_code"),
        "multi_gpu_exit_code": multi_s.get("exit_code"),
        "stage_status": {
            "cuda": cuda_s,
            "cpu": cpu_s,
            "single_gpu": single_s,
            "multi_gpu": multi_s,
        },
    }

    hallucinations = {
        "path": {"count": len(path_items), "items": path_items},
        "version": {"count": len(version_items), "items": version_items},
        "capability": {"count": len(capability_items), "items": capability_items},
    }

    any_hallucination = any(x["count"] > 0 for x in hallucinations.values())
    status = "success" if not any_hallucination else "failure"
    exit_code = 0 if not any_hallucination else 1

    failure_category = "unknown"
    if rep_err is not None:
        failure_category = "missing_report" if rep_err == "missing" else "invalid_json"
    elif hallucinations["path"]["count"] > 0:
        failure_category = "path_hallucination"
    elif hallucinations["version"]["count"] > 0:
        failure_category = "version_hallucination"
    elif hallucinations["capability"]["count"] > 0:
        failure_category = "capability_hallucination"

    payload = {
        "status": status,
        "exit_code": exit_code,
        "stage": "hallucination",
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "failure_category": failure_category,
        "error_excerpt": tail_text(log_path),
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_f.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
