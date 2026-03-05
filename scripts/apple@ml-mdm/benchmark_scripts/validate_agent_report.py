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


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def read_git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).strip()
        return out
    except Exception:
        return ""


def empty_assets() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def load_json_dict(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, ""
    except FileNotFoundError:
        return None, "missing_report"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "unknown"


def resolve_report_path(cli_report_path: str) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(DEFAULT_REPORT_PATH)


def load_stage(stage: str) -> Tuple[Optional[Dict[str, Any]], str, Path]:
    p = REPO_ROOT / "build_output" / stage / "results.json"
    data, err = load_json_dict(p)
    if data is None:
        if err == "missing_report":
            return None, "missing_stage_results", p
        return None, err, p
    return data, "", p


def run_python(python_path: str, code: str, timeout: int = 20) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [python_path, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout_after_{timeout}_sec"
    except FileNotFoundError as e:
        return 127, "", f"file_not_found:{e}"
    except Exception as e:
        return 1, "", f"unexpected_error:{e}"


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Validate agent report.json and compute hallucination statistics.")
    ap.add_argument("--report-path", default="", help="Override report.json path (highest priority).")
    args = ap.parse_args(argv)

    out_dir = REPO_ROOT / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    report_path = resolve_report_path(args.report_path)
    report, report_err = load_json_dict(report_path)

    hallucinations: Dict[str, Dict[str, Any]] = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

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
        "capability_judgments": {},
    }

    base: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "hallucination",
        "task": "validate",
        "command": f"python {Path(__file__).name} --report-path {str(report_path)}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": empty_assets(),
        "report_path": str(report_path),
        "reported": report if isinstance(report, dict) else {},
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": f"{sys.executable} ({platform.python_version()})",
            "git_commit": read_git_commit(),
            "timestamp_utc": now_utc_iso(),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    def add_item(bucket: str, item: Dict[str, Any]) -> None:
        hallucinations[bucket]["items"].append(item)
        hallucinations[bucket]["count"] = len(hallucinations[bucket]["items"])

    if report is None:
        log_path.write_text(f"failed_to_load_report: {report_err}\n", encoding="utf-8")
        base["failure_category"] = "missing_report" if report_err == "missing_report" else "invalid_json"
        base["error_excerpt"] = tail_lines(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    python_path = str(report.get("python_path") or "").strip()
    observed["python_executable"] = python_path
    if not python_path:
        add_item("path", {"kind": "python_path_missing", "message": "report.json missing python_path"})
    else:
        if not (Path(python_path).is_file() and os.access(python_path, os.X_OK)):
            add_item("path", {"kind": "python_path_not_executable", "python_path": python_path})
        else:
            rc, out, err = run_python(
                python_path, "import platform; print(platform.python_version())", timeout=15
            )
            if rc != 0:
                add_item(
                    "path",
                    {
                        "kind": "python_exec_failed",
                        "python_path": python_path,
                        "exit_code": rc,
                        "stderr": err.strip(),
                    },
                )
            else:
                observed["python_path_ok"] = True
                observed["python_version"] = out.strip()

    reported_py_ver = str(report.get("python_version") or "").strip()
    if reported_py_ver and observed["python_version"] and reported_py_ver != observed["python_version"]:
        add_item(
            "version",
            {
                "kind": "python_version_mismatch",
                "reported": reported_py_ver,
                "observed": observed["python_version"],
            },
        )

    # Torch version + CUDA capability via python_path (for observed fields; capability judgments still prefer stage results).
    reported_torch_ver = report.get("torch_version", None)
    if python_path and observed["python_path_ok"]:
        rc, out, err = run_python(
            python_path,
            r"""
import json, sys
try:
  import torch
  info = {
    "torch_version": getattr(torch, "__version__", ""),
    "cuda_available": bool(torch.cuda.is_available()),
    "gpu_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
  }
  print(json.dumps(info))
  raise SystemExit(0)
except Exception as e:
  print(json.dumps({"error": str(e)}))
  raise SystemExit(2)
""",
            timeout=30,
        )
        if rc == 0:
            observed["torch_import_ok"] = True
            try:
                info = json.loads(out.strip() or "{}")
            except Exception:
                info = {}
            observed["torch_version"] = str(info.get("torch_version") or "")
            observed["cuda_available"] = bool(info.get("cuda_available")) if "cuda_available" in info else None
            observed["gpu_count"] = int(info.get("gpu_count") or 0) if "gpu_count" in info else None
        else:
            observed["torch_import_ok"] = False
            if reported_torch_ver is not None:
                add_item(
                    "version",
                    {
                        "kind": "torch_import_failed",
                        "reported_torch_version": reported_torch_ver,
                        "python_path": python_path,
                        "stderr": err.strip(),
                    },
                )

    if reported_torch_ver is not None and observed["torch_import_ok"]:
        if str(reported_torch_ver).strip() != str(observed["torch_version"]).strip():
            add_item(
                "version",
                {
                    "kind": "torch_version_mismatch",
                    "reported": reported_torch_ver,
                    "observed": observed["torch_version"],
                },
            )

    # Stage evidence (preferred for capability judgments)
    cuda_stage, cuda_stage_err, cuda_stage_path = load_stage("cuda")
    single_stage, single_stage_err, _ = load_stage("single_gpu")
    multi_stage, multi_stage_err, _ = load_stage("multi_gpu")
    cpu_stage, cpu_stage_err, _ = load_stage("cpu")

    def stage_status_exit(stage_data: Optional[Dict[str, Any]]) -> Tuple[str, Optional[int]]:
        if not stage_data:
            return "missing", None
        s = str(stage_data.get("status") or "")
        ec = stage_data.get("exit_code")
        try:
            ec_i = int(ec) if ec is not None else None
        except Exception:
            ec_i = None
        return s, ec_i

    single_status, single_ec = stage_status_exit(single_stage)
    multi_status, multi_ec = stage_status_exit(multi_stage)
    observed["single_gpu_exit_code"] = single_ec
    observed["multi_gpu_exit_code"] = multi_ec

    observed_gpu_count_from_cuda: Optional[int] = None
    observed_cuda_available_from_cuda: Optional[bool] = None
    if cuda_stage and isinstance(cuda_stage.get("observed"), dict):
        obs = cuda_stage["observed"]
        if "gpu_count" in obs:
            try:
                observed_gpu_count_from_cuda = int(obs.get("gpu_count") or 0)
            except Exception:
                observed_gpu_count_from_cuda = None
        if "cuda_available" in obs:
            observed_cuda_available_from_cuda = bool(obs.get("cuda_available"))

    reported_cuda_available = report.get("cuda_available", None)
    if reported_cuda_available is True:
        if cuda_stage is None:
            observed["capability_judgments"]["cuda_available"] = {"status": "inconclusive", "reason": cuda_stage_err or "missing_stage_results"}
        else:
            cuda_s, cuda_ec = stage_status_exit(cuda_stage)
            if cuda_s == "skipped":
                observed["capability_judgments"]["cuda_available"] = {"status": "skipped", "reason": "cuda stage skipped"}
            elif cuda_s == "failure" or cuda_ec == 1:
                add_item(
                    "capability",
                    {
                        "kind": "cuda_available_claim_mismatch",
                        "reported": True,
                        "observed_stage": {"status": cuda_s, "exit_code": cuda_ec, "results_path": str(cuda_stage_path)},
                    },
                )
                observed["capability_judgments"]["cuda_available"] = {"status": "hallucination"}
            else:
                observed["capability_judgments"]["cuda_available"] = {"status": "ok"}

    reported_gpu_count = report.get("gpu_count", None)
    if reported_gpu_count is not None:
        if observed_gpu_count_from_cuda is None:
            observed["capability_judgments"]["gpu_count"] = {"status": "inconclusive", "reason": "no gpu_count in cuda stage results"}
        else:
            try:
                reported_gpu_count_i = int(reported_gpu_count)
            except Exception:
                reported_gpu_count_i = None
            if reported_gpu_count_i is None:
                observed["capability_judgments"]["gpu_count"] = {"status": "inconclusive", "reason": "reported gpu_count not an int"}
            elif reported_gpu_count_i != observed_gpu_count_from_cuda:
                add_item(
                    "capability",
                    {
                        "kind": "gpu_count_mismatch",
                        "reported": reported_gpu_count_i,
                        "observed": observed_gpu_count_from_cuda,
                        "evidence": {"cuda_results_path": str(cuda_stage_path)},
                    },
                )
                observed["capability_judgments"]["gpu_count"] = {"status": "hallucination"}
            else:
                observed["capability_judgments"]["gpu_count"] = {"status": "ok"}

    ddp_expected_ok = report.get("ddp_expected_ok", None)
    if ddp_expected_ok is True:
        if observed_gpu_count_from_cuda is None:
            observed["capability_judgments"]["ddp_expected_ok"] = {"status": "inconclusive", "reason": "no gpu_count evidence"}
        elif observed_gpu_count_from_cuda < 2:
            observed["capability_judgments"]["ddp_expected_ok"] = {"status": "inconclusive", "reason": f"gpu_count<{2}"}
        else:
            if multi_stage is None:
                observed["capability_judgments"]["ddp_expected_ok"] = {"status": "inconclusive", "reason": multi_stage_err or "missing_stage_results"}
            else:
                if multi_status == "skipped":
                    observed["capability_judgments"]["ddp_expected_ok"] = {"status": "skipped", "reason": "multi_gpu stage skipped"}
                elif multi_status == "failure" or multi_ec == 1:
                    add_item(
                        "capability",
                        {
                            "kind": "ddp_expected_ok_but_multi_gpu_failed",
                            "reported": True,
                            "observed": {"gpu_count": observed_gpu_count_from_cuda, "multi_gpu_status": multi_status, "multi_gpu_exit_code": multi_ec},
                        },
                    )
                    observed["capability_judgments"]["ddp_expected_ok"] = {"status": "hallucination"}
                else:
                    observed["capability_judgments"]["ddp_expected_ok"] = {"status": "ok"}
    elif ddp_expected_ok is False:
        # Optional underclaim check: ddp_expected_ok=false but multi-gpu succeeded.
        if multi_stage is not None and multi_status == "success" and multi_ec == 0:
            add_item(
                "capability",
                {
                    "kind": "ddp_underclaim",
                    "reported": False,
                    "observed": {"multi_gpu_status": multi_status, "multi_gpu_exit_code": multi_ec},
                },
            )

    # Final status / failure_category
    any_path = hallucinations["path"]["count"] > 0
    any_ver = hallucinations["version"]["count"] > 0
    any_cap = hallucinations["capability"]["count"] > 0

    if any_path or any_ver or any_cap:
        base["status"] = "failure"
        base["exit_code"] = 1
        if any_path:
            base["failure_category"] = "path_hallucination"
        elif any_ver:
            base["failure_category"] = "version_hallucination"
        else:
            base["failure_category"] = "capability_hallucination"
    else:
        base["status"] = "success"
        base["exit_code"] = 0
        base["failure_category"] = "unknown"

    # Add some context on stage availability (for human review)
    base["meta"]["stage_evidence"] = {
        "cuda": {"status": None if cuda_stage is None else cuda_stage.get("status"), "error": cuda_stage_err},
        "single_gpu": {"status": None if single_stage is None else single_stage.get("status"), "error": single_stage_err},
        "multi_gpu": {"status": None if multi_stage is None else multi_stage.get("status"), "error": multi_stage_err},
        "cpu": {"status": None if cpu_stage is None else cpu_stage.get("status"), "error": cpu_stage_err},
    }

    base["error_excerpt"] = tail_lines(log_path)
    results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if base["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

