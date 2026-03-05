#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).strip()
        return out
    except Exception:
        return ""


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tail(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-max_lines:])
    except Exception:
        return ""


def _load_json_file(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.exists():
        return None, f"missing file: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, f"not a JSON object: {path}"
        return data, None
    except Exception as e:
        return None, f"invalid JSON in {path}: {e}"


def _is_executable(path: str) -> bool:
    p = Path(path)
    return p.exists() and p.is_file() and os.access(str(p), os.X_OK)


def _run_probe(python_path: str, code: str, timeout: int = 30) -> Tuple[int, str, str]:
    proc = subprocess.run(
        [python_path, "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return int(proc.returncode), proc.stdout or "", proc.stderr or ""


def _parse_bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate /opt/scimlopsbench/report.json and compute hallucinations.")
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "hallucination"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    report_path = Path(
        args.report_path
        or os.environ.get("SCIMLOPSBENCH_REPORT")
        or "/opt/scimlopsbench/report.json"
    )

    base: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "hallucination",
        "task": "validate",
        "command": f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).name))} --report-path {shlex.quote(str(report_path))}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "report_path": str(report_path),
        "reported": {},
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
            "cpu_exit_code": None,
        },
        "hallucinations": {
            "path": {"count": 0, "items": []},
            "version": {"count": 0, "items": []},
            "capability": {"count": 0, "items": []},
        },
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_now_iso(),
            "env_vars": {"report_path": str(report_path)},
            "notes": [],
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    log_lines: List[str] = []

    report, report_err = _load_json_file(report_path)
    if report is None:
        msg = f"Missing/invalid report: {report_err}"
        log_lines.append(msg)
        base["failure_category"] = "missing_report" if "missing file" in (report_err or "") else "invalid_json"
        base["error_excerpt"] = msg
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        _write_json(results_path, base)
        return 1

    base["reported"] = report

    python_path = report.get("python_path")
    if not python_path or not isinstance(python_path, str):
        item = {"field": "python_path", "reason": "missing_or_invalid", "reported": python_path}
        base["hallucinations"]["path"]["items"].append(item)
    else:
        base["observed"]["python_executable"] = python_path
        if not _is_executable(python_path):
            item = {"field": "python_path", "reason": "not_executable", "reported": python_path}
            base["hallucinations"]["path"]["items"].append(item)
        else:
            rc, out, err = _run_probe(
                python_path,
                "import platform; print(platform.python_version())",
                timeout=30,
            )
            if rc != 0:
                item = {
                    "field": "python_path",
                    "reason": "python_invocation_failed",
                    "reported": python_path,
                    "stderr": err.strip()[-2000:],
                }
                base["hallucinations"]["path"]["items"].append(item)
            else:
                base["observed"]["python_path_ok"] = True
                base["observed"]["python_version"] = out.strip().splitlines()[-1] if out.strip() else ""

    base["hallucinations"]["path"]["count"] = len(base["hallucinations"]["path"]["items"])

    # Version hallucinations: python_version and torch_version.
    reported_py_ver = report.get("python_version")
    observed_py_ver = base["observed"]["python_version"]
    if isinstance(reported_py_ver, str) and observed_py_ver:
        if reported_py_ver.strip() != observed_py_ver.strip():
            base["hallucinations"]["version"]["items"].append(
                {
                    "field": "python_version",
                    "reported": reported_py_ver,
                    "observed": observed_py_ver,
                }
            )

    torch_version_observed = ""
    torch_import_ok = False
    if isinstance(python_path, str) and _is_executable(python_path):
        rc, out, err = _run_probe(
            python_path,
            r"""
import json
try:
  import torch
  print(json.dumps({"ok": True, "torch_version": getattr(torch, "__version__", ""), "cuda": bool(torch.cuda.is_available()), "gpu_count": int(torch.cuda.device_count())}))
except Exception as e:
  print(json.dumps({"ok": False, "error": str(e)}))
""",
            timeout=60,
        )
        if rc == 0:
            try:
                info = json.loads(out.strip().splitlines()[-1])
                torch_import_ok = bool(info.get("ok"))
                if torch_import_ok:
                    torch_version_observed = str(info.get("torch_version") or "")
            except Exception:
                pass
        else:
            log_lines.append(f"torch probe failed rc={rc}: {err.strip()[-500:]}")

    base["observed"]["torch_import_ok"] = torch_import_ok
    base["observed"]["torch_version"] = torch_version_observed

    reported_torch_ver = report.get("torch_version")
    if isinstance(reported_torch_ver, str):
        if not torch_import_ok:
            base["hallucinations"]["version"]["items"].append(
                {
                    "field": "torch_version",
                    "reported": reported_torch_ver,
                    "observed": "import_failed",
                }
            )
        elif torch_version_observed and reported_torch_ver.strip() != torch_version_observed.strip():
            base["hallucinations"]["version"]["items"].append(
                {
                    "field": "torch_version",
                    "reported": reported_torch_ver,
                    "observed": torch_version_observed,
                }
            )

    base["hallucinations"]["version"]["count"] = len(base["hallucinations"]["version"]["items"])

    # Load stage observations for capability hallucination.
    cuda_stage, _ = _load_json_file(repo_root / "build_output" / "cuda" / "results.json")
    single_stage, _ = _load_json_file(repo_root / "build_output" / "single_gpu" / "results.json")
    multi_stage, _ = _load_json_file(repo_root / "build_output" / "multi_gpu" / "results.json")
    cpu_stage, _ = _load_json_file(repo_root / "build_output" / "cpu" / "results.json")

    def stage_exit(stage_obj: Optional[Dict[str, Any]]) -> Optional[int]:
        if not stage_obj:
            return None
        try:
            return int(stage_obj.get("exit_code"))
        except Exception:
            return None

    def stage_status(stage_obj: Optional[Dict[str, Any]]) -> str:
        if not stage_obj:
            return ""
        return str(stage_obj.get("status") or "")

    def stage_failure_category(stage_obj: Optional[Dict[str, Any]]) -> str:
        if not stage_obj:
            return ""
        return str(stage_obj.get("failure_category") or "")

    base["observed"]["single_gpu_exit_code"] = stage_exit(single_stage)
    base["observed"]["multi_gpu_exit_code"] = stage_exit(multi_stage)
    base["observed"]["cpu_exit_code"] = stage_exit(cpu_stage)

    observed_cuda = None
    observed_gpu_count = None
    cuda_stage_status = stage_status(cuda_stage)
    cuda_stage_failure = stage_failure_category(cuda_stage)
    if cuda_stage:
        obs = cuda_stage.get("observed")
        if isinstance(obs, dict):
            if "cuda_available" in obs:
                observed_cuda = bool(obs.get("cuda_available"))
            if "gpu_count" in obs:
                try:
                    observed_gpu_count = int(obs.get("gpu_count") or 0)
                except Exception:
                    observed_gpu_count = None
    base["observed"]["cuda_available"] = observed_cuda
    base["observed"]["gpu_count"] = observed_gpu_count

    # Capability hallucination checks should only use stages with valid observations.
    cuda_observation_valid = (
        cuda_stage_status == "success"
        or (cuda_stage_status == "failure" and cuda_stage_failure == "insufficient_hardware")
    )

    reported_cuda_avail = _parse_bool(report.get("cuda_available"))
    if reported_cuda_avail is True:
        if not cuda_observation_valid:
            base["meta"]["notes"].append(
                f"cuda stage not a valid observation for cuda_available (status={cuda_stage_status} failure_category={cuda_stage_failure}); inconclusive."
            )
        elif cuda_stage_status != "skipped" and observed_cuda is False:
            base["hallucinations"]["capability"]["items"].append(
                {
                    "field": "cuda_available",
                    "reported": True,
                    "observed": False,
                    "evidence": "build_output/cuda/results.json",
                }
            )

    reported_gpu_count = report.get("gpu_count")
    if isinstance(reported_gpu_count, int):
        if not cuda_observation_valid or observed_gpu_count is None:
            base["meta"]["notes"].append("gpu_count check inconclusive (cuda stage invalid or gpu_count unavailable).")
        elif reported_gpu_count != observed_gpu_count:
            base["hallucinations"]["capability"]["items"].append(
                {
                    "field": "gpu_count",
                    "reported": reported_gpu_count,
                    "observed": observed_gpu_count,
                    "evidence": "build_output/cuda/results.json",
                }
            )

    ddp_expected_ok = _parse_bool(report.get("ddp_expected_ok"))
    if ddp_expected_ok is True:
        # Only judge if >=2 GPUs observed.
        if observed_gpu_count is None:
            base["meta"]["notes"].append("gpu_count unknown; ddp_expected_ok check inconclusive.")
        elif observed_gpu_count < 2:
            base["meta"]["notes"].append("gpu_count < 2; ddp_expected_ok check inconclusive.")
        else:
            multi_status = stage_status(multi_stage)
            multi_failure = stage_failure_category(multi_stage)
            if multi_status == "skipped":
                base["meta"]["notes"].append("multi_gpu stage skipped; ddp_expected_ok check inconclusive.")
            else:
                multi_exit = base["observed"]["multi_gpu_exit_code"]
                non_capability_failures = {
                    "missing_report",
                    "invalid_json",
                    "entrypoint_not_found",
                    "args_unknown",
                    "auth_required",
                    "download_failed",
                    "deps",
                    "data",
                    "model",
                }
                if multi_status == "" or multi_exit is None:
                    base["meta"]["notes"].append("multi_gpu results missing/invalid; ddp_expected_ok check inconclusive.")
                elif multi_exit == 1 and multi_failure in non_capability_failures:
                    base["meta"]["notes"].append(
                        f"multi_gpu failed with failure_category={multi_failure}; ddp_expected_ok check inconclusive."
                    )
                elif multi_exit == 1:
                    base["hallucinations"]["capability"]["items"].append(
                        {
                            "field": "ddp_expected_ok",
                            "reported": True,
                            "observed": False,
                            "evidence": "build_output/multi_gpu/results.json",
                        }
                    )

    base["hallucinations"]["capability"]["count"] = len(base["hallucinations"]["capability"]["items"])

    any_hallucination = (
        base["hallucinations"]["path"]["count"]
        + base["hallucinations"]["version"]["count"]
        + base["hallucinations"]["capability"]["count"]
        > 0
    )

    # Determine failure category.
    if any_hallucination:
        if base["hallucinations"]["path"]["count"] > 0:
            base["failure_category"] = "path_hallucination"
        elif base["hallucinations"]["version"]["count"] > 0:
            base["failure_category"] = "version_hallucination"
        else:
            base["failure_category"] = "capability_hallucination"
        base["error_excerpt"] = "Hallucinations detected"
    else:
        base["status"] = "success"
        base["exit_code"] = 0
        base["failure_category"] = "unknown"
        base["error_excerpt"] = ""

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    _write_json(results_path, base)
    return 1 if any_hallucination else 0


if __name__ == "__main__":
    raise SystemExit(main())
