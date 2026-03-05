#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).replace(microsecond=0).isoformat()


def safe_git_commit(root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return (cp.stdout or "").strip()
    except Exception:
        return ""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(DEFAULT_REPORT_PATH)


def read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception as e:
        return None, f"read_error:{type(e).__name__}:{e}"
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except Exception as e:
        return None, f"invalid_json:{type(e).__name__}:{e}"


def read_stage_results(root: Path, stage: str) -> Tuple[Optional[dict], Optional[str]]:
    p = root / "build_output" / stage / "results.json"
    data, err = read_json(p)
    if err is not None:
        return None, err
    return data, None


def run_python(python_exe: str, code: str, timeout_sec: int = 60) -> Tuple[int, str, str]:
    cp = subprocess.run(
        [python_exe, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout_sec,
    )
    return cp.returncode, cp.stdout.strip(), cp.stderr.strip()


def add_item(items: List[dict], kind: str, message: str, evidence: Optional[dict] = None) -> None:
    obj: Dict[str, Any] = {"kind": kind, "message": message}
    if evidence is not None:
        obj["evidence"] = evidence
    items.append(obj)


def stage_has_actionable_capability_observation(stage_res: Optional[dict]) -> bool:
    """
    Capability hallucinations must be judged only when we have a meaningful stage execution result.
    Treat 'skipped', 'insufficient_hardware', and precheck failures as inconclusive.
    """
    if not isinstance(stage_res, dict):
        return False
    if stage_res.get("status") == "skipped":
        return False
    if stage_res.get("skip_reason") == "insufficient_hardware":
        return False
    meta = stage_res.get("meta") if isinstance(stage_res.get("meta"), dict) else {}
    decision_reason = meta.get("decision_reason") if isinstance(meta.get("decision_reason"), str) else ""
    if "precheck failed" in decision_reason:
        return False
    cmd = stage_res.get("command") if isinstance(stage_res.get("command"), str) else ""
    if not cmd.strip():
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate agent report.json and compute hallucination statistics.")
    ap.add_argument("--report-path", default=None, help="Override report path (else SCIMLOPSBENCH_REPORT or default).")
    args = ap.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "hallucination"
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    command_str = f"{sys.executable} {Path(__file__).as_posix()} --report-path {report_path}"

    reported: Dict[str, Any] = {}
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
        "notes": [],
    }

    hallucinations = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    report, report_err = read_json(report_path)
    if report_err is not None or not isinstance(report, dict):
        msg = f"report_missing_or_invalid: {report_err}"
        write_text(log_path, msg + "\n")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "hallucination",
            "task": "validate",
            "command": command_str,
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
            "meta": {"git_commit": safe_git_commit(root), "timestamp_utc": utc_now()},
            "failure_category": "missing_report" if report_err == "missing_report" else "invalid_json",
            "error_excerpt": msg,
        }
        write_json(results_path, payload)
        return 1

    reported = dict(report)
    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path:
        add_item(hallucinations["path"]["items"], "python_path", "python_path missing from report")
    else:
        observed["python_executable"] = python_path
        p = Path(python_path)
        if not (p.exists() and os.access(p, os.X_OK)):
            add_item(
                hallucinations["path"]["items"],
                "python_path",
                f"python_path not executable: {python_path}",
                evidence={"path": python_path},
            )
        else:
            rc, out, err = run_python(python_path, "import platform; print(platform.python_version())", timeout_sec=30)
            if rc != 0:
                add_item(
                    hallucinations["path"]["items"],
                    "python_path",
                    f"python_path failed to run: rc={rc}",
                    evidence={"stderr": err[-2000:], "stdout": out[-2000:]},
                )
            else:
                observed["python_path_ok"] = True
                observed["python_version"] = out.strip()

    reported_py_ver = report.get("python_version")
    if isinstance(reported_py_ver, str) and reported_py_ver and observed["python_version"]:
        if reported_py_ver.strip() != observed["python_version"].strip():
            add_item(
                hallucinations["version"]["items"],
                "python_version",
                f"Reported python_version {reported_py_ver} != observed {observed['python_version']}",
            )

    reported_torch_ver = report.get("torch_version")
    if isinstance(python_path, str) and python_path and observed["python_path_ok"]:
        rc, out, err = run_python(python_path, "import torch; print(torch.__version__)", timeout_sec=60)
        if rc != 0:
            if isinstance(reported_torch_ver, str) and reported_torch_ver:
                add_item(
                    hallucinations["version"]["items"],
                    "torch_version",
                    f"Reported torch_version={reported_torch_ver} but import torch failed",
                    evidence={"stderr": err[-2000:], "stdout": out[-2000:]},
                )
        else:
            observed["torch_import_ok"] = True
            observed["torch_version"] = out.strip()
            if isinstance(reported_torch_ver, str) and reported_torch_ver and reported_torch_ver.strip() != out.strip():
                add_item(
                    hallucinations["version"]["items"],
                    "torch_version",
                    f"Reported torch_version {reported_torch_ver} != observed {out.strip()}",
                )

    # Read observed capability evidence from benchmark stage outputs (if present).
    cuda_res, cuda_err = read_stage_results(root, "cuda")
    if cuda_err is None and isinstance(cuda_res, dict):
        obs = cuda_res.get("observed") if isinstance(cuda_res.get("observed"), dict) else {}
        if isinstance(obs.get("cuda_available"), bool):
            observed["cuda_available"] = obs["cuda_available"]
        if isinstance(obs.get("gpu_count"), int):
            observed["gpu_count"] = obs["gpu_count"]
        if isinstance(cuda_res.get("status"), str) and cuda_res.get("status") == "skipped":
            observed["notes"].append("cuda_stage_skipped")
    else:
        observed["notes"].append(f"cuda_stage_unavailable:{cuda_err}")

    single_res, single_err = read_stage_results(root, "single_gpu")
    if single_err is None and isinstance(single_res, dict):
        if isinstance(single_res.get("exit_code"), int):
            observed["single_gpu_exit_code"] = single_res["exit_code"]
        if single_res.get("status") == "skipped":
            observed["notes"].append("single_gpu_stage_skipped")
    else:
        observed["notes"].append(f"single_gpu_stage_unavailable:{single_err}")

    multi_res, multi_err = read_stage_results(root, "multi_gpu")
    if multi_err is None and isinstance(multi_res, dict):
        if isinstance(multi_res.get("exit_code"), int):
            observed["multi_gpu_exit_code"] = multi_res["exit_code"]
        if multi_res.get("status") == "skipped":
            observed["notes"].append("multi_gpu_stage_skipped")
    else:
        observed["notes"].append(f"multi_gpu_stage_unavailable:{multi_err}")

    # Capability hallucination rules (only when evidence exists and stage not skipped).
    reported_cuda = report.get("cuda_available")
    if isinstance(reported_cuda, bool) and reported_cuda is True and isinstance(cuda_res, dict):
        if cuda_res.get("status") != "skipped" and (cuda_res.get("status") == "failure" or cuda_res.get("exit_code") == 1):
            add_item(
                hallucinations["capability"]["items"],
                "cuda_available",
                "Report says cuda_available=true but CUDA check failed",
                evidence={"cuda_results": "build_output/cuda/results.json"},
            )

    reported_gpu_count = report.get("gpu_count")
    if isinstance(reported_gpu_count, int) and isinstance(observed.get("gpu_count"), int):
        if reported_gpu_count != observed["gpu_count"]:
            add_item(
                hallucinations["capability"]["items"],
                "gpu_count",
                f"Reported gpu_count={reported_gpu_count} != observed gpu_count={observed['gpu_count']}",
                evidence={"cuda_results": "build_output/cuda/results.json"},
            )

    ddp_expected_ok = report.get("ddp_expected_ok")
    if isinstance(ddp_expected_ok, bool) and ddp_expected_ok is True:
        gpu_count_obs = observed.get("gpu_count") if isinstance(observed.get("gpu_count"), int) else None
        if gpu_count_obs is None:
            observed["notes"].append("ddp_expected_ok_inconclusive:gpu_count_unknown")
        elif gpu_count_obs < 2:
            observed["notes"].append("ddp_expected_ok_inconclusive:gpu_count_lt_2")
        else:
            if stage_has_actionable_capability_observation(multi_res):
                if multi_res.get("status") == "failure" or multi_res.get("exit_code") == 1:
                    add_item(
                        hallucinations["capability"]["items"],
                        "ddp_expected_ok",
                        "Report says ddp_expected_ok=true but multi-GPU run failed with >=2 GPUs",
                        evidence={"multi_gpu_results": "build_output/multi_gpu/results.json"},
                    )
            else:
                observed["notes"].append("ddp_expected_ok_inconclusive:multi_gpu_unavailable_or_inconclusive")

    # Finalize counts + status.
    for k in ("path", "version", "capability"):
        hallucinations[k]["count"] = len(hallucinations[k]["items"])

    any_hallucination = any(hallucinations[k]["count"] > 0 for k in ("path", "version", "capability"))
    status = "failure" if any_hallucination else "success"
    exit_code = 1 if any_hallucination else 0

    if hallucinations["path"]["count"] > 0:
        failure_category = "path_hallucination"
    elif hallucinations["version"]["count"] > 0:
        failure_category = "version_hallucination"
    elif hallucinations["capability"]["count"] > 0:
        failure_category = "capability_hallucination"
    else:
        failure_category = "unknown"

    log_lines = [
        f"[hallucination] report_path={report_path}",
        f"[hallucination] python_path_ok={observed['python_path_ok']}",
        f"[hallucination] path={hallucinations['path']['count']} version={hallucinations['version']['count']} capability={hallucinations['capability']['count']}",
    ]
    if observed.get("notes"):
        log_lines.append(f"[hallucination] notes={observed['notes']}")
    write_text(log_path, "\n".join(log_lines) + "\n")

    payload = {
        "status": status,
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": command_str,
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
        "meta": {"git_commit": safe_git_commit(root), "timestamp_utc": utc_now()},
        "failure_category": failure_category,
        "error_excerpt": "",
    }
    if status == "failure":
        payload["error_excerpt"] = "Hallucinations detected" if any_hallucination else ""

    write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
