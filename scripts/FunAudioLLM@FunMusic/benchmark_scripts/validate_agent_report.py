#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace")), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _is_executable(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def _resolve_report_path(cli_path: Optional[str]) -> Path:
    if cli_path:
        return Path(cli_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def _load_stage_result(repo_root: Path, stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = repo_root / "build_output" / stage / "results.json"
    if not path.exists():
        return None, f"missing: {path}"
    data, err = _read_json(path)
    if data is None:
        return None, f"invalid_json: {path}: {err}"
    return data, None


def _run_python(python_exe: str, code: str, timeout: int = 30) -> Tuple[int, str, str]:
    proc = subprocess.run([python_exe, "-c", code], capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate /opt/scimlopsbench/report.json and compute hallucination statistics.")
    parser.add_argument("--report-path", help="Override report path (else SCIMLOPSBENCH_REPORT or default).")
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)
    report, report_err = _read_json(report_path)
    report_exists = report_path.exists()
    report_parse_ok = report is not None

    path_items: List[Dict[str, Any]] = []
    version_items: List[Dict[str, Any]] = []
    capability_items: List[Dict[str, Any]] = []
    warnings: List[str] = []

    if report is None:
        kind = "missing_report" if not report_exists else "invalid_json"
        path_items.append({"kind": kind, "detail": f"{report_path}: {report_err or 'missing'}"})
        report = {}

    python_path = report.get("python_path")
    python_exe = python_path if isinstance(python_path, str) else ""

    python_path_ok = bool(python_exe) and _is_executable(Path(python_exe))
    if not python_exe:
        path_items.append({"kind": "python_path_missing", "detail": "report.python_path missing"})
    elif not python_path_ok:
        path_items.append({"kind": "python_path_not_executable", "detail": f"{python_exe} is not executable"})

    observed: Dict[str, Any] = {
        "python_path_ok": python_path_ok,
        "python_executable": python_exe,
        "python_version": None,
        "torch_import_ok": False,
        "torch_version": None,
        "cuda_available": None,
        "gpu_count": None,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
    }

    # Probe python version / torch version from the reported python_path.
    if python_path_ok:
        rc, out, err = _run_python(python_exe, "import platform; print(platform.python_version())")
        if rc != 0:
            path_items.append({"kind": "python_exec_failed", "detail": (err or out).strip()[:4000]})
        else:
            observed["python_version"] = out.strip().splitlines()[-1] if out.strip() else None

        rc, out, err = _run_python(
            python_exe,
            "import json; import torch; print(torch.__version__)",
        )
        if rc != 0:
            observed["torch_import_ok"] = False
            version_items.append({"kind": "torch_import_failed", "detail": (err or out).strip()[:4000]})
        else:
            observed["torch_import_ok"] = True
            observed["torch_version"] = out.strip().splitlines()[-1] if out.strip() else None

        rc, out, err = _run_python(
            python_exe,
            "import json, torch; print(json.dumps({'cuda': bool(torch.cuda.is_available()), 'count': int(torch.cuda.device_count())}))",
        )
        if rc == 0:
            try:
                info = json.loads(out.strip().splitlines()[-1])
                observed["cuda_available"] = bool(info.get("cuda"))
                observed["gpu_count"] = int(info.get("count"))
            except Exception as e:
                warnings.append(f"failed to parse torch cuda probe: {type(e).__name__}: {e}")
        else:
            warnings.append(f"torch cuda probe failed: {(err or out).strip()[:2000]}")

    # Reported versions
    reported_python_version = report.get("python_version")
    if python_path_ok and reported_python_version and observed["python_version"]:
        if str(reported_python_version) != str(observed["python_version"]):
            version_items.append(
                {
                    "kind": "python_version_mismatch",
                    "detail": f"reported={reported_python_version} observed={observed['python_version']}",
                }
            )

    reported_torch_version = report.get("torch_version")
    if python_path_ok and reported_torch_version:
        if not observed["torch_import_ok"]:
            version_items.append({"kind": "torch_version_unverifiable", "detail": "torch import failed"})
        elif observed["torch_version"] and str(reported_torch_version) != str(observed["torch_version"]):
            version_items.append(
                {
                    "kind": "torch_version_mismatch",
                    "detail": f"reported={reported_torch_version} observed={observed['torch_version']}",
                }
            )

    # Stage-based observations for capability checks.
    cuda_stage, cuda_err = _load_stage_result(repo_root, "cuda")
    if cuda_err:
        warnings.append(f"cuda stage unavailable: {cuda_err}")
    single_stage, single_err = _load_stage_result(repo_root, "single_gpu")
    if single_err:
        warnings.append(f"single_gpu stage unavailable: {single_err}")
    multi_stage, multi_err = _load_stage_result(repo_root, "multi_gpu")
    if multi_err:
        warnings.append(f"multi_gpu stage unavailable: {multi_err}")

    if cuda_stage:
        observed_cuda = (cuda_stage.get("observed") or {}) if isinstance(cuda_stage.get("observed"), dict) else {}
        if observed["cuda_available"] is None and "cuda_available" in observed_cuda:
            observed["cuda_available"] = bool(observed_cuda.get("cuda_available"))
        if observed["gpu_count"] is None and "gpu_count" in observed_cuda:
            try:
                observed["gpu_count"] = int(observed_cuda.get("gpu_count"))
            except Exception:
                pass

    if single_stage:
        observed["single_gpu_exit_code"] = int(single_stage.get("exit_code", 1))
    if multi_stage:
        observed["multi_gpu_exit_code"] = int(multi_stage.get("exit_code", 1))

    # Capability hallucination rules (only judge capabilities with valid observations).
    reported_cuda_available = report.get("cuda_available")
    if isinstance(reported_cuda_available, bool) and cuda_stage:
        cuda_observed_ok = cuda_stage.get("exit_code", 1) == 0 and cuda_stage.get("status") == "success"
        if reported_cuda_available and not cuda_observed_ok:
            capability_items.append({"kind": "cuda_available_mismatch", "detail": "report.cuda_available=true but cuda check failed"})

    reported_gpu_count = report.get("gpu_count")
    if reported_gpu_count is not None and observed.get("gpu_count") is not None:
        try:
            if int(reported_gpu_count) != int(observed["gpu_count"]):
                capability_items.append(
                    {
                        "kind": "gpu_count_mismatch",
                        "detail": f"reported={reported_gpu_count} observed={observed['gpu_count']}",
                    }
                )
        except Exception:
            warnings.append("gpu_count not comparable (non-int values)")

    ddp_expected_ok = report.get("ddp_expected_ok")
    if isinstance(ddp_expected_ok, bool):
        multi_status = multi_stage.get("status") if multi_stage else None
        if multi_stage and multi_status == "skipped":
            warnings.append("multi_gpu stage skipped; ddp capability marked inconclusive")
        else:
            if ddp_expected_ok:
                if observed.get("gpu_count") is not None and int(observed["gpu_count"]) < 2:
                    warnings.append("ddp_expected_ok reported true but <2 GPUs observed; inconclusive")
                elif multi_stage and int(multi_stage.get("exit_code", 1)) != 0:
                    capability_items.append(
                        {
                            "kind": "ddp_expected_ok_but_failed",
                            "detail": "report.ddp_expected_ok=true but multi_gpu stage failed",
                        }
                    )
            else:
                # Optional under-claim: if ddp_expected_ok is false but multi-GPU succeeded.
                if multi_stage and int(multi_stage.get("exit_code", 1)) == 0:
                    capability_items.append(
                        {
                            "kind": "ddp_underclaim",
                            "detail": "report.ddp_expected_ok=false but multi_gpu stage succeeded",
                        }
                    )

    hallucinations = {
        "path": {"count": len(path_items), "items": path_items},
        "version": {"count": len(version_items), "items": version_items},
        "capability": {"count": len(capability_items), "items": capability_items},
    }

    any_hallucination = any(v["count"] > 0 for v in hallucinations.values())
    exit_code = 1 if any_hallucination or not report_exists or not report_parse_ok else 0

    failure_category = "unknown"
    if not report_exists:
        failure_category = "missing_report"
    elif not report_parse_ok:
        failure_category = "invalid_json"
    elif hallucinations["path"]["count"] > 0:
        failure_category = "path_hallucination"
    elif hallucinations["version"]["count"] > 0:
        failure_category = "version_hallucination"
    elif hallucinations["capability"]["count"] > 0:
        failure_category = "capability_hallucination"

    log_lines = [
        f"[hallucination] timestamp_utc={_utc_now_iso()}",
        f"[hallucination] report_path={report_path}",
        f"[hallucination] python_path={python_exe}",
        f"[hallucination] counts: path={hallucinations['path']['count']} version={hallucinations['version']['count']} capability={hallucinations['capability']['count']}",
    ]
    if warnings:
        log_lines.append("[hallucination] warnings:")
        log_lines.extend(f"  - {w}" for w in warnings[:200])
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    results = {
        "status": "failure" if exit_code != 0 else "success",
        "exit_code": exit_code,
        "stage": "hallucination",
        "report_path": str(report_path),
        "reported": report,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "timestamp_utc": _utc_now_iso(),
            "git_commit": None,
            "warnings": warnings,
        },
        "failure_category": failure_category if exit_code != 0 else "unknown",
        "error_excerpt": "",
    }

    try:
        results["meta"]["git_commit"] = (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True).strip()
        )
    except Exception:
        results["meta"]["git_commit"] = None

    if exit_code != 0:
        # Build a short excerpt of the most relevant issue(s).
        excerpt_parts: List[str] = []
        for group in ("path", "version", "capability"):
            for item in hallucinations[group]["items"][:10]:
                excerpt_parts.append(f"{group}:{item.get('kind')}: {item.get('detail')}")
        results["error_excerpt"] = "\n".join(excerpt_parts)[:8000]

    _write_json(results_path, results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
