#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def _utc_timestamp() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_text(path: Path, content: str) -> None:
    _safe_mkdir(path.parent)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    _safe_mkdir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tail(path: Path, max_lines: int = 240) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        return "".join(lines[-max_lines:])
    except Exception:
        return ""


def resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return DEFAULT_REPORT_PATH


def load_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    if not path.exists():
        return None, f"missing: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, f"invalid_json_root: {path}"
        return data, None
    except Exception as e:
        return None, f"invalid_json: {path}: {e}"


def is_executable(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def run_python(python_exe: str, code: str, timeout_sec: int = 20) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [python_exe, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def stage_results_path(stage: str) -> Path:
    return REPO_ROOT / "build_output" / stage / "results.json"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Validate agent report.json and compute hallucination statistics.")
    ap.add_argument("--report-path", default="", help="Override report path.")
    args = ap.parse_args(argv)

    out_dir = REPO_ROOT / "build_output" / "hallucination"
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    _safe_mkdir(out_dir)

    header = (
        f"stage=hallucination\n"
        f"repo={REPO_ROOT}\n"
        f"out_dir={out_dir}\n"
        f"timestamp_utc={_utc_timestamp()}\n"
        f"runner_python={sys.executable}\n"
    )
    _write_text(log_path, header)

    report_path = resolve_report_path(args.report_path or None)
    report, report_err = load_json(report_path)

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(msg.rstrip() + "\n")

    result: Dict[str, Any] = {
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
        "report_path": str(report_path),
        "reported": report if report else {},
        "observed": {
            "python_path_ok": False,
            "python_executable": "",
            "python_version": "",
            "torch_import_ok": False,
            "torch_version": "",
            "cuda_available": None,
            "gpu_count": None,
            "single_gpu_exit_code": None,
            "multi_gpu_exit_code": None,
        },
        "hallucinations": {
            "path": {"count": 0, "items": []},
            "version": {"count": 0, "items": []},
            "capability": {"count": 0, "items": []},
        },
        "meta": {
            "timestamp_utc": _utc_timestamp(),
            "notes": [],
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    if report is None:
        log(f"ERROR: {report_err}")
        result["hallucinations"]["path"]["items"].append({"type": "missing_report", "detail": report_err})
        result["hallucinations"]["path"]["count"] = 1
        result["failure_category"] = "missing_report" if (report_err or "").startswith("missing") else "invalid_json"
        result["error_excerpt"] = _tail(log_path)
        _write_json(results_path, result)
        return 1

    # ---- Path checks ----
    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        result["hallucinations"]["path"]["items"].append({"type": "python_path_missing", "detail": "report.python_path missing/empty"})
    else:
        p = Path(python_path)
        if not is_executable(p):
            result["hallucinations"]["path"]["items"].append(
                {"type": "python_path_not_executable", "detail": f"python_path not executable: {python_path}"}
            )
        else:
            rc, out, err = run_python(python_path, "import platform; print(platform.python_version())")
            if rc != 0:
                result["hallucinations"]["path"]["items"].append(
                    {"type": "python_invocation_failed", "detail": f"{python_path} -c failed: {err}"}
                )
            else:
                result["observed"]["python_path_ok"] = True
                result["observed"]["python_executable"] = python_path
                result["observed"]["python_version"] = out.strip()

    result["hallucinations"]["path"]["count"] = len(result["hallucinations"]["path"]["items"])

    # ---- Version checks ----
    reported_py_ver = report.get("python_version")
    if result["observed"]["python_version"] and isinstance(reported_py_ver, str) and reported_py_ver.strip():
        if result["observed"]["python_version"] != reported_py_ver.strip():
            result["hallucinations"]["version"]["items"].append(
                {
                    "type": "python_version_mismatch",
                    "detail": f"reported={reported_py_ver.strip()} observed={result['observed']['python_version']}",
                }
            )

    reported_torch_ver = report.get("torch_version")
    if isinstance(reported_torch_ver, str) and reported_torch_ver.strip():
        if isinstance(python_path, str) and python_path.strip() and is_executable(Path(python_path)):
            rc, out, err = run_python(python_path, "import torch; print(torch.__version__)")
            if rc != 0:
                result["hallucinations"]["version"]["items"].append(
                    {"type": "torch_import_failed", "detail": f"import torch failed: {err}"}
                )
                result["observed"]["torch_import_ok"] = False
            else:
                result["observed"]["torch_import_ok"] = True
                result["observed"]["torch_version"] = out.strip()
                if out.strip() != reported_torch_ver.strip():
                    result["hallucinations"]["version"]["items"].append(
                        {
                            "type": "torch_version_mismatch",
                            "detail": f"reported={reported_torch_ver.strip()} observed={out.strip()}",
                        }
                    )
        else:
            result["meta"]["notes"].append("Skipping torch version check: python_path invalid.")

    result["hallucinations"]["version"]["count"] = len(result["hallucinations"]["version"]["items"])

    # ---- Capability checks (based on observed stage results only) ----
    cuda_res, cuda_err = load_json(stage_results_path("cuda"))
    if cuda_res and isinstance(cuda_res.get("observed"), dict):
        result["observed"]["cuda_available"] = cuda_res["observed"].get("cuda_available")
        result["observed"]["gpu_count"] = cuda_res["observed"].get("gpu_count")
    elif cuda_err:
        result["meta"]["notes"].append(f"CUDA stage results unavailable ({cuda_err}); capability checks may be inconclusive.")

    single_res, single_err = load_json(stage_results_path("single_gpu"))
    if single_res:
        result["observed"]["single_gpu_exit_code"] = single_res.get("exit_code")
    elif single_err:
        result["meta"]["notes"].append(f"single_gpu stage results unavailable ({single_err}).")

    multi_res, multi_err = load_json(stage_results_path("multi_gpu"))
    if multi_res:
        result["observed"]["multi_gpu_exit_code"] = multi_res.get("exit_code")
    elif multi_err:
        result["meta"]["notes"].append(f"multi_gpu stage results unavailable ({multi_err}).")

    # Only judge capabilities when we have valid observations.
    reported_cuda = report.get("cuda_available")
    if isinstance(reported_cuda, bool) and result["observed"]["cuda_available"] is not None:
        if reported_cuda and not bool(result["observed"]["cuda_available"]):
            result["hallucinations"]["capability"]["items"].append(
                {"type": "cuda_claim_mismatch", "detail": "report.cuda_available=true but cuda stage failed"}
            )

    reported_gpu_count = report.get("gpu_count")
    if isinstance(reported_gpu_count, int) and result["observed"]["gpu_count"] is not None:
        if int(reported_gpu_count) != int(result["observed"]["gpu_count"]):
            result["hallucinations"]["capability"]["items"].append(
                {
                    "type": "gpu_count_mismatch",
                    "detail": f"reported={reported_gpu_count} observed={result['observed']['gpu_count']}",
                }
            )

    ddp_expected_ok = report.get("ddp_expected_ok")
    if isinstance(ddp_expected_ok, bool) and ddp_expected_ok:
        # Only judge if we observed >=2 GPUs and multi-gpu stage ran (not skipped).
        observed_gpu_count = result["observed"]["gpu_count"]
        if isinstance(observed_gpu_count, int) and observed_gpu_count >= 2 and multi_res:
            if multi_res.get("status") == "skipped":
                result["meta"]["notes"].append("DDP claim inconclusive: multi_gpu stage skipped.")
            else:
                fc = str(multi_res.get("failure_category", "unknown"))
                # If the benchmark couldn't actually launch the repo entrypoint, do not count as a capability miss.
                if fc in {"args_unknown", "entrypoint_not_found", "missing_report", "invalid_json"}:
                    result["meta"]["notes"].append(
                        f"DDP claim inconclusive: multi_gpu did not run a valid launch (failure_category={fc})."
                    )
                else:
                    if multi_res.get("exit_code") == 1:
                        result["hallucinations"]["capability"]["items"].append(
                            {
                                "type": "ddp_expected_ok_but_failed",
                                "detail": "report.ddp_expected_ok=true but multi_gpu failed",
                            }
                        )
        else:
            result["meta"]["notes"].append("DDP claim inconclusive: insufficient GPU observation or missing multi_gpu results.")

    result["hallucinations"]["capability"]["count"] = len(result["hallucinations"]["capability"]["items"])

    # ---- Final outcome ----
    any_h = (
        result["hallucinations"]["path"]["count"]
        + result["hallucinations"]["version"]["count"]
        + result["hallucinations"]["capability"]["count"]
    )
    if any_h > 0:
        result["status"] = "failure"
        result["exit_code"] = 1
        if result["hallucinations"]["path"]["count"] > 0:
            result["failure_category"] = "path_hallucination"
        elif result["hallucinations"]["version"]["count"] > 0:
            result["failure_category"] = "version_hallucination"
        else:
            result["failure_category"] = "capability_hallucination"
        result["error_excerpt"] = _tail(log_path)
        _write_json(results_path, result)
        return 1

    result["status"] = "success"
    result["exit_code"] = 0
    result["failure_category"] = "unknown"
    result["error_excerpt"] = ""
    _write_json(results_path, result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        out_dir = REPO_ROOT / "build_output" / "hallucination"
        log_path = out_dir / "log.txt"
        results_path = out_dir / "results.json"
        _safe_mkdir(out_dir)
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
            f.write("\nFATAL:\n")
            f.write(f"{type(e).__name__}: {e}\n")
            f.write(traceback.format_exc() + "\n")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "hallucination",
            "task": "validate",
            "command": f"{sys.executable} benchmark_scripts/validate_agent_report.py --report-path {os.environ.get('SCIMLOPSBENCH_REPORT', str(DEFAULT_REPORT_PATH))}",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "report_path": os.environ.get("SCIMLOPSBENCH_REPORT", str(DEFAULT_REPORT_PATH)),
            "reported": {},
            "observed": {},
            "hallucinations": {
                "path": {"count": 0, "items": []},
                "version": {"count": 0, "items": []},
                "capability": {"count": 0, "items": []},
            },
            "failure_category": "unknown",
            "error_excerpt": _tail(log_path),
        }
        _write_json(results_path, payload)
        raise SystemExit(1)
