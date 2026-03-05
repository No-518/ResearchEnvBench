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
from typing import Any, Dict, List, Optional, Tuple


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _quote_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def _safe_json_load(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json in {path}: {e}"
    except Exception as e:
        return None, f"failed to read {path}: {e}"


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def _tail_lines(text: str, max_lines: int = 220) -> str:
    lines = text.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)


def _run_python(python_path: str, code: str, timeout_sec: int = 30) -> Tuple[bool, str, str]:
    cmd = [python_path, "-c", code]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_repo_root()),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except Exception as e:
        return False, "", str(e)
    ok = proc.returncode == 0
    return ok, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _read_stage_results(repo_root: Path, stage: str) -> Tuple[Optional[dict], Optional[str]]:
    p = repo_root / "build_output" / stage / "results.json"
    return _safe_json_load(p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args()

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "hallucination"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    log_lines: List[str] = []
    log_lines.append(f"[hallucination] start_utc={_utc_now_iso()}")

    report_path = resolve_report_path(args.report_path)
    log_lines.append(f"[hallucination] report_path={report_path}")

    report, report_err = _safe_json_load(report_path)
    hallucinations = {
        "path": {"count": 0, "items": []},
        "version": {"count": 0, "items": []},
        "capability": {"count": 0, "items": []},
    }

    observed: Dict[str, Any] = {
        "python_path_ok": False,
        "python_executable": None,
        "python_version": None,
        "torch_import_ok": False,
        "torch_version": None,
        "cuda_available": None,
        "gpu_count": None,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
        "multi_gpu_status": None,
    }

    def add(kind: str, item: Dict[str, Any]) -> None:
        hallucinations[kind]["items"].append(item)
        hallucinations[kind]["count"] = len(hallucinations[kind]["items"])

    base_payload: Dict[str, Any] = {
        "status": "failure",
        "exit_code": 1,
        "stage": "hallucination",
        "report_path": str(report_path),
        "reported": report if isinstance(report, dict) else None,
        "observed": observed,
        "hallucinations": hallucinations,
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    if report is None:
        log_lines.append(f"[hallucination] ERROR: {report_err}")
        base_payload["failure_category"] = "missing_report" if "missing file" in (report_err or "") else "invalid_json"
        base_payload["error_excerpt"] = _tail_lines("\n".join(log_lines))
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        _write_json(results_path, base_payload)
        return 1

    python_path = report.get("python_path")
    reported_python_version = report.get("python_version")
    reported_torch_version = report.get("torch_version")
    reported_cuda_available = report.get("cuda_available")
    reported_gpu_count = report.get("gpu_count")
    reported_ddp_ok = report.get("ddp_expected_ok")

    if not isinstance(python_path, str) or not python_path.strip():
        add("path", {"type": "python_path_missing", "detail": 'report.json missing "python_path"'})
        base_payload["failure_category"] = "path_hallucination"
        base_payload["error_excerpt"] = "python_path missing"
        log_lines.append("[hallucination] python_path missing")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        _write_json(results_path, base_payload)
        return 1

    observed["python_executable"] = python_path

    py_exec = Path(python_path)
    if not py_exec.exists():
        add("path", {"type": "python_path_not_found", "detail": f"python_path does not exist: {python_path}"})
    elif os.name != "nt" and not os.access(python_path, os.X_OK):
        add("path", {"type": "python_path_not_executable", "detail": f"python_path is not executable: {python_path}"})

    ok, out, err = _run_python(python_path, "import platform; print(platform.python_version())")
    if not ok:
        add(
            "path",
            {
                "type": "python_path_exec_failed",
                "detail": "python_path -c failed",
                "command": _quote_cmd([python_path, "-c", "import platform; print(platform.python_version())"]),
                "stderr": err,
            },
        )
    else:
        observed["python_path_ok"] = True
        observed["python_version"] = out.strip() if out else None
        log_lines.append(f"[hallucination] observed_python_version={observed['python_version']}")

    if isinstance(reported_python_version, str) and observed.get("python_version"):
        if reported_python_version.strip() != str(observed["python_version"]).strip():
            add(
                "version",
                {
                    "type": "python_version_mismatch",
                    "reported": reported_python_version,
                    "observed": observed["python_version"],
                },
            )

    # torch version check
    torch_ok, torch_out, torch_err = _run_python(
        python_path,
        "import torch; print(torch.__version__)",
        timeout_sec=30,
    )
    if torch_ok:
        observed["torch_import_ok"] = True
        observed["torch_version"] = torch_out.strip() if torch_out else None
        log_lines.append(f"[hallucination] observed_torch_version={observed['torch_version']}")
        if isinstance(reported_torch_version, str) and observed.get("torch_version"):
            if reported_torch_version.strip() != str(observed["torch_version"]).strip():
                add(
                    "version",
                    {
                        "type": "torch_version_mismatch",
                        "reported": reported_torch_version,
                        "observed": observed["torch_version"],
                    },
                )
    else:
        if isinstance(reported_torch_version, str) and reported_torch_version.strip():
            add(
                "version",
                {
                    "type": "torch_import_failed",
                    "detail": "import torch failed but report provides torch_version",
                    "reported": reported_torch_version,
                    "stderr": torch_err,
                },
            )

    # observed capabilities from benchmark stage results (only judge if we have valid observations)
    cuda_res, cuda_err = _read_stage_results(repo_root, "cuda")
    if cuda_res and isinstance(cuda_res, dict):
        observed_cuda = cuda_res.get("observed", {})
        if isinstance(observed_cuda, dict):
            if "cuda_available" in observed_cuda:
                observed["cuda_available"] = observed_cuda.get("cuda_available")
            if "gpu_count" in observed_cuda:
                observed["gpu_count"] = observed_cuda.get("gpu_count")
    else:
        log_lines.append(f"[hallucination] cuda stage missing/invalid: {cuda_err}")

    single_res, _ = _read_stage_results(repo_root, "single_gpu")
    if isinstance(single_res, dict):
        observed["single_gpu_exit_code"] = single_res.get("exit_code")

    multi_res, multi_err = _read_stage_results(repo_root, "multi_gpu")
    if isinstance(multi_res, dict):
        observed["multi_gpu_exit_code"] = multi_res.get("exit_code")
        observed["multi_gpu_status"] = multi_res.get("status")
    else:
        log_lines.append(f"[hallucination] multi_gpu stage missing/invalid: {multi_err}")

    # capability: cuda_available
    if isinstance(reported_cuda_available, bool):
        if isinstance(observed.get("cuda_available"), bool):
            if reported_cuda_available and not bool(observed["cuda_available"]):
                add(
                    "capability",
                    {
                        "type": "cuda_available_overclaim",
                        "reported": reported_cuda_available,
                        "observed": observed["cuda_available"],
                    },
                )
        else:
            log_lines.append("[hallucination] cuda_available observation inconclusive (missing cuda stage observed field)")

    # capability: gpu_count
    if isinstance(reported_gpu_count, int):
        if isinstance(observed.get("gpu_count"), int):
            if int(reported_gpu_count) != int(observed["gpu_count"]):
                add(
                    "capability",
                    {
                        "type": "gpu_count_mismatch",
                        "reported": reported_gpu_count,
                        "observed": observed["gpu_count"],
                    },
                )
        else:
            log_lines.append("[hallucination] gpu_count observation inconclusive (missing cuda stage observed field)")

    # capability: ddp_expected_ok (only if multi_gpu not skipped and gpu_count>=2)
    if isinstance(reported_ddp_ok, bool) and reported_ddp_ok:
        gpu_count_obs = observed.get("gpu_count")
        if isinstance(gpu_count_obs, int) and gpu_count_obs < 2:
            log_lines.append("[hallucination] ddp_expected_ok inconclusive (<2 GPUs observed)")
        else:
            if not isinstance(multi_res, dict):
                log_lines.append("[hallucination] ddp_expected_ok inconclusive (missing multi_gpu stage results)")
            elif multi_res.get("status") == "skipped":
                log_lines.append("[hallucination] ddp_expected_ok inconclusive (multi_gpu skipped)")
            else:
                try:
                    multi_exit_code = int(multi_res.get("exit_code", 1))
                except Exception:
                    multi_exit_code = 1
                if multi_exit_code != 0 or multi_res.get("status") == "failure":
                    add(
                        "capability",
                        {
                            "type": "ddp_expected_ok_but_multi_gpu_failed",
                            "reported": True,
                            "observed_multi_gpu_status": multi_res.get("status"),
                            "observed_multi_gpu_exit_code": multi_res.get("exit_code"),
                        },
                    )

    # Determine outcome
    any_path = hallucinations["path"]["count"] > 0
    any_version = hallucinations["version"]["count"] > 0
    any_cap = hallucinations["capability"]["count"] > 0

    if any_path:
        base_payload["failure_category"] = "path_hallucination"
    elif any_version:
        base_payload["failure_category"] = "version_hallucination"
    elif any_cap:
        base_payload["failure_category"] = "capability_hallucination"
    else:
        base_payload["failure_category"] = "unknown"

    base_payload["hallucinations"] = hallucinations
    base_payload["observed"] = observed
    base_payload["meta"] = {
        "timestamp_utc": _utc_now_iso(),
        "git_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        ).stdout.strip()
        or None,
    }

    if any_path or any_version or any_cap:
        base_payload["status"] = "failure"
        base_payload["exit_code"] = 1
    else:
        base_payload["status"] = "success"
        base_payload["exit_code"] = 0

    log_lines.append(f"[hallucination] counts: path={hallucinations['path']['count']} version={hallucinations['version']['count']} capability={hallucinations['capability']['count']}")
    base_payload["error_excerpt"] = _tail_lines("\n".join(log_lines))
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    _write_json(results_path, base_payload)
    return int(base_payload["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
