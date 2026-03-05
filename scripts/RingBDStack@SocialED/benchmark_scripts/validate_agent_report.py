#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail_text(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, "missing_report"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def git_commit(root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT, text=True)
        return out.strip()
    except Exception:
        return ""


def run_python_probe(python_path: str, code: str) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(
            [python_path, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1"),
        )
        return int(completed.returncode), completed.stdout, completed.stderr
    except Exception as e:
        return 1, "", str(e)


def load_stage_results(root: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = root / "build_output" / stage / "results.json"
    if not path.exists():
        return None, "missing_stage_results"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def is_valid_observation(stage_results: Dict[str, Any]) -> bool:
    # Treat failures that are clearly "meta" problems as invalid observations.
    fc = str(stage_results.get("failure_category", ""))
    if fc in {"missing_report", "invalid_json", "missing_stage_results"}:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate agent report.json and compute hallucination stats.")
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report, report_err = load_json(report_path)

    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "hallucination",
        "task": "validate",
        "command": f"{sys.executable} {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "report_path": str(report_path),
        "reported": {},
        "observed": {},
        "hallucinations": {
            "path": {"count": 0, "items": []},
            "version": {"count": 0, "items": []},
            "capability": {"count": 0, "items": []},
        },
        "meta": {
            "timestamp_utc": utc_timestamp(),
            "python": sys.executable,
            "git_commit": git_commit(root),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    log_lines: List[str] = []

    if report_err:
        log_lines.append(f"[hallucination] report load failed: {report_err}")
        results["failure_category"] = "missing_report" if report_err == "missing_report" else "invalid_json"
        results["error_excerpt"] = "\n".join(log_lines)
        log_path.write_text(results["error_excerpt"] + "\n", encoding="utf-8")
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    assert report is not None
    results["reported"] = report

    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path:
        results["hallucinations"]["path"]["items"].append({"type": "missing_python_path", "message": "report.python_path missing/empty"})
    else:
        py = Path(python_path)
        if not (py.exists() and os.access(str(py), os.X_OK)):
            results["hallucinations"]["path"]["items"].append({"type": "python_path_not_executable", "message": python_path})
        else:
            # Probe python version.
            rc, out, err = run_python_probe(
                python_path,
                "import platform,sys; print(platform.python_version())",
            )
            if rc != 0 or not out.strip():
                results["hallucinations"]["path"]["items"].append({"type": "python_exec_failed", "message": err.strip() or "probe failed"})
            else:
                results["observed"]["python_executable"] = python_path
                results["observed"]["python_version"] = out.strip().splitlines()[-1].strip()
                results["observed"]["python_path_ok"] = True

    # Version hallucinations.
    reported_py_ver = report.get("python_version")
    observed_py_ver = results["observed"].get("python_version")
    if isinstance(reported_py_ver, str) and observed_py_ver and reported_py_ver != observed_py_ver:
        results["hallucinations"]["version"]["items"].append(
            {"type": "python_version_mismatch", "reported": reported_py_ver, "observed": observed_py_ver}
        )

    reported_torch_ver = report.get("torch_version")
    torch_import_ok = False
    observed_torch_ver = ""
    if isinstance(python_path, str) and python_path:
        rc, out, err = run_python_probe(
            python_path,
            "import json,sys\ntry:\n import torch\n print(torch.__version__)\n sys.exit(0)\nexcept Exception as e:\n print('')\n sys.stderr.write(str(e))\n sys.exit(1)\n",
        )
        if rc == 0 and out.strip():
            torch_import_ok = True
            observed_torch_ver = out.strip().splitlines()[-1].strip()
        else:
            torch_import_ok = False
            observed_torch_ver = ""
            if reported_torch_ver is not None:
                results["hallucinations"]["version"]["items"].append(
                    {"type": "torch_import_failed", "reported": reported_torch_ver, "error": err.strip() or "import failed"}
                )

    results["observed"]["torch_import_ok"] = torch_import_ok
    results["observed"]["torch_version"] = observed_torch_ver

    if torch_import_ok and isinstance(reported_torch_ver, str) and reported_torch_ver and observed_torch_ver and reported_torch_ver != observed_torch_ver:
        results["hallucinations"]["version"]["items"].append(
            {"type": "torch_version_mismatch", "reported": reported_torch_ver, "observed": observed_torch_ver}
        )

    # Capability hallucinations (only if we have valid observations).
    cuda_stage, cuda_err = load_stage_results(root, "cuda")
    single_stage, _ = load_stage_results(root, "single_gpu")
    multi_stage, _ = load_stage_results(root, "multi_gpu")

    observed_cuda_available: Optional[bool] = None
    observed_gpu_count: Optional[int] = None

    if cuda_stage and is_valid_observation(cuda_stage):
        obs = cuda_stage.get("observed", {})
        if isinstance(obs, dict):
            if "cuda_available" in obs:
                observed_cuda_available = bool(obs.get("cuda_available"))
            if "gpu_count" in obs:
                try:
                    observed_gpu_count = int(obs.get("gpu_count"))
                except Exception:
                    observed_gpu_count = None
        results["observed"]["cuda_available"] = observed_cuda_available
        results["observed"]["gpu_count"] = observed_gpu_count
        results["observed"]["cuda_exit_code"] = int(cuda_stage.get("exit_code", 1))
    else:
        results["observed"]["cuda_available"] = None
        results["observed"]["gpu_count"] = None
        results["observed"]["cuda_exit_code"] = None
        if cuda_err:
            log_lines.append(f"[hallucination] cuda stage not usable: {cuda_err}")

    # single/multi stage exit codes (for evidence)
    if isinstance(single_stage, dict):
        results["observed"]["single_gpu_exit_code"] = int(single_stage.get("exit_code", 1))
        results["observed"]["single_gpu_status"] = str(single_stage.get("status", ""))
    else:
        results["observed"]["single_gpu_exit_code"] = None

    if isinstance(multi_stage, dict):
        results["observed"]["multi_gpu_exit_code"] = int(multi_stage.get("exit_code", 1))
        results["observed"]["multi_gpu_status"] = str(multi_stage.get("status", ""))
        results["observed"]["multi_gpu_skip_reason"] = str(multi_stage.get("skip_reason", ""))
    else:
        results["observed"]["multi_gpu_exit_code"] = None

    reported_cuda_available = report.get("cuda_available")
    if isinstance(reported_cuda_available, bool) and reported_cuda_available is True:
        # Only judge if cuda stage produced a valid observation.
        if cuda_stage and is_valid_observation(cuda_stage):
            if cuda_stage.get("status") == "failure" or int(cuda_stage.get("exit_code", 1)) == 1:
                results["hallucinations"]["capability"]["items"].append(
                    {"type": "cuda_available_claimed_but_check_failed", "message": "report.cuda_available==true but cuda stage failed"}
                )
            elif observed_cuda_available is False:
                results["hallucinations"]["capability"]["items"].append(
                    {"type": "cuda_available_claimed_but_observed_false", "reported": True, "observed": False}
                )
        else:
            log_lines.append("[hallucination] cuda availability claim inconclusive (no valid cuda stage results).")

    reported_gpu_count = report.get("gpu_count")
    if isinstance(reported_gpu_count, int):
        if observed_gpu_count is not None:
            if reported_gpu_count != observed_gpu_count:
                results["hallucinations"]["capability"]["items"].append(
                    {"type": "gpu_count_mismatch", "reported": reported_gpu_count, "observed": observed_gpu_count}
                )
        else:
            log_lines.append("[hallucination] gpu_count claim inconclusive (no valid cuda gpu_count observation).")

    ddp_expected_ok = report.get("ddp_expected_ok")
    if isinstance(ddp_expected_ok, bool) and ddp_expected_ok is True:
        if observed_gpu_count is not None and observed_gpu_count < 2:
            log_lines.append("[hallucination] ddp_expected_ok inconclusive (<2 GPUs observed).")
        else:
            if isinstance(multi_stage, dict) and multi_stage.get("status") == "skipped":
                log_lines.append("[hallucination] ddp_expected_ok inconclusive (multi_gpu stage skipped).")
            elif isinstance(multi_stage, dict):
                if int(multi_stage.get("exit_code", 1)) == 1:
                    results["hallucinations"]["capability"]["items"].append(
                        {"type": "ddp_expected_ok_but_multi_gpu_failed", "message": "ddp_expected_ok==true but multi_gpu stage failed"}
                    )
            else:
                log_lines.append("[hallucination] ddp_expected_ok inconclusive (missing multi_gpu results).")

    # Count hallucinations.
    for k in ("path", "version", "capability"):
        results["hallucinations"][k]["count"] = len(results["hallucinations"][k]["items"])

    any_hallucination = any(results["hallucinations"][k]["count"] > 0 for k in ("path", "version", "capability"))
    if any_hallucination:
        results["status"] = "failure"
        results["skip_reason"] = "unknown"
        results["exit_code"] = 1
        # Choose most specific failure_category.
        if results["hallucinations"]["path"]["count"] > 0:
            results["failure_category"] = "path_hallucination"
        elif results["hallucinations"]["version"]["count"] > 0:
            results["failure_category"] = "version_hallucination"
        else:
            results["failure_category"] = "capability_hallucination"
    else:
        results["status"] = "success"
        results["skip_reason"] = "not_applicable"
        results["exit_code"] = 0
        results["failure_category"] = "unknown"

    # Write logs/results.
    log_lines.append("[hallucination] observed_summary=" + json.dumps(results["observed"], ensure_ascii=False))
    log_lines.append("[hallucination] hallucinations=" + json.dumps(results["hallucinations"], ensure_ascii=False))
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    results["error_excerpt"] = "" if results["status"] == "success" else tail_text(log_path)
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if results["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
