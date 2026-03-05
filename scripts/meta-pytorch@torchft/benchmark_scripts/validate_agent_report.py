#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _report_path(cli_report_path: str | None) -> Path:
    if cli_report_path and cli_report_path.strip():
        return Path(cli_report_path.strip())
    env_report = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_report and env_report.strip():
        return Path(env_report.strip())
    return Path("/opt/scimlopsbench/report.json")


def _tail(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return "\n".join(txt.splitlines()[-max_lines:]).strip()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:
        return ""


def _read_stage_results(repo_root: Path, stage: str) -> tuple[dict[str, Any] | None, str]:
    p = repo_root / "build_output" / stage / "results.json"
    if not p.exists():
        return None, "missing"
    try:
        return json.loads(p.read_text(encoding="utf-8")), "ok"
    except Exception:
        return None, "invalid_json"


def _parse_exit_code(value: Any, default: int = 1) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "hallucination"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _report_path(args.report_path)

    path_items: list[dict[str, Any]] = []
    version_items: list[dict[str, Any]] = []
    capability_items: list[dict[str, Any]] = []

    observed: dict[str, Any] = {
        "python_path_ok": False,
        "python_executable": "",
        "python_version": "",
        "torch_import_ok": False,
        "torch_version": "",
        "cuda_available": False,
        "gpu_count": 0,
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
        "cpu_exit_code": None,
    }

    reported: dict[str, Any] = {}
    failure_category = "not_applicable"

    with log_path.open("w", encoding="utf-8") as log_fp:
        log_fp.write(f"[hallucination] utc_start={_utc_timestamp()}\n")
        log_fp.write(f"[hallucination] report_path={report_path}\n")

        if not report_path.exists():
            failure_category = "missing_report"
            log_fp.write("[hallucination] error: report missing\n")
        else:
            try:
                reported = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception as e:
                failure_category = "invalid_json"
                log_fp.write(f"[hallucination] error: invalid report json: {e}\n")
            else:
                python_path = str(reported.get("python_path", "")).strip()
                if not python_path:
                    failure_category = "path_hallucination"
                    path_items.append(
                        {"type": "missing_python_path", "detail": "report.python_path missing/empty"}
                    )
                else:
                    p = Path(python_path)
                    if not p.exists() or not os.access(str(p), os.X_OK):
                        failure_category = "path_hallucination"
                        path_items.append(
                            {
                                "type": "python_path_not_executable",
                                "detail": f"python_path={python_path} not executable",
                            }
                        )
                    else:
                        observed["python_executable"] = python_path
                        # Path hallucination: python -c "import platform" fails
                        cp = subprocess.run(
                            [python_path, "-c", "import platform; print(platform.python_version())"],
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        if cp.returncode != 0:
                            failure_category = "path_hallucination"
                            path_items.append(
                                {
                                    "type": "python_invocation_failed",
                                    "detail": f"python_path invocation failed: rc={cp.returncode}",
                                }
                            )
                            log_fp.write(cp.stdout)
                            log_fp.write(cp.stderr)
                        else:
                            observed["python_path_ok"] = True
                            observed["python_version"] = cp.stdout.strip().splitlines()[-1]

                            reported_pyver = str(reported.get("python_version", "")).strip()
                            if reported_pyver and reported_pyver != observed["python_version"]:
                                version_items.append(
                                    {
                                        "type": "python_version_mismatch",
                                        "reported": reported_pyver,
                                        "observed": observed["python_version"],
                                    }
                                )

                            # Version hallucination: torch import/version mismatch
                            cp2 = subprocess.run(
                                [
                                    python_path,
                                    "-c",
                                    "import json; import torch; print(json.dumps({'torch_version': getattr(torch,'__version__',''), 'cuda_available': bool(torch.cuda.is_available()), 'gpu_count': int(torch.cuda.device_count())}))",
                                ],
                                text=True,
                                capture_output=True,
                                check=False,
                            )
                            if cp2.returncode != 0:
                                version_items.append(
                                    {
                                        "type": "torch_import_failed",
                                        "reported_torch_version": str(reported.get("torch_version", "")),
                                        "detail": "import torch failed in reported python_path",
                                    }
                                )
                                log_fp.write(cp2.stdout)
                                log_fp.write(cp2.stderr)
                            else:
                                observed["torch_import_ok"] = True
                                try:
                                    info = json.loads(cp2.stdout.strip().splitlines()[-1])
                                except Exception:
                                    info = {}
                                observed["torch_version"] = str(info.get("torch_version", ""))
                                # Also provide direct observed cuda/gpu_count (used if stage results missing)
                                observed["cuda_available"] = bool(info.get("cuda_available", False))
                                observed["gpu_count"] = int(info.get("gpu_count", 0) or 0)

                                reported_torch = str(reported.get("torch_version", "")).strip()
                                if reported_torch and reported_torch != observed["torch_version"]:
                                    version_items.append(
                                        {
                                            "type": "torch_version_mismatch",
                                            "reported": reported_torch,
                                            "observed": observed["torch_version"],
                                        }
                                    )

        # Load stage results for capability checks (must be based on real execution).
        cuda_res, cuda_state = _read_stage_results(repo_root, "cuda")
        if cuda_state == "ok" and isinstance(cuda_res, dict):
            try:
                obs = cuda_res.get("observed", {})
                observed["cuda_available"] = bool(obs.get("cuda_available", False))
                observed["gpu_count"] = int(obs.get("gpu_count", 0) or 0)
            except Exception:
                pass

        single_res, single_state = _read_stage_results(repo_root, "single_gpu")
        if single_state == "ok" and isinstance(single_res, dict):
            observed["single_gpu_exit_code"] = _parse_exit_code(single_res.get("exit_code", 1), default=1)
        multi_res, multi_state = _read_stage_results(repo_root, "multi_gpu")
        if multi_state == "ok" and isinstance(multi_res, dict):
            observed["multi_gpu_exit_code"] = _parse_exit_code(multi_res.get("exit_code", 1), default=1)
        cpu_res, cpu_state = _read_stage_results(repo_root, "cpu")
        if cpu_state == "ok" and isinstance(cpu_res, dict):
            observed["cpu_exit_code"] = _parse_exit_code(cpu_res.get("exit_code", 1), default=1)

        # Capability hallucinations: only when we have valid observations and the stage was included (not skipped).
        if reported:
            rep_cuda = reported.get("cuda_available", None)
            if rep_cuda is True and cuda_res and cuda_res.get("status") != "skipped":
                if observed["cuda_available"] is False:
                    capability_items.append(
                        {
                            "type": "cuda_available_overclaim",
                            "reported": True,
                            "observed": False,
                            "evidence_stage": "cuda",
                        }
                    )

            rep_gpu_count = reported.get("gpu_count", None)
            if isinstance(rep_gpu_count, int) and cuda_res and cuda_res.get("status") != "skipped":
                if observed["gpu_count"] != rep_gpu_count:
                    capability_items.append(
                        {
                            "type": "gpu_count_mismatch",
                            "reported": rep_gpu_count,
                            "observed": observed["gpu_count"],
                            "evidence_stage": "cuda",
                        }
                    )

            ddp_expected_ok = reported.get("ddp_expected_ok", None)
            if ddp_expected_ok is True:
                # If hardware insufficient, mark inconclusive (not hallucination).
                if observed["gpu_count"] >= 2 and multi_res and multi_res.get("status") != "skipped":
                    multi_exit = _parse_exit_code(multi_res.get("exit_code", 1), default=1)
                    if multi_exit != 0:
                        capability_items.append(
                            {
                                "type": "ddp_expected_ok_but_multi_failed",
                                "reported": True,
                                "observed_multi_exit_code": multi_exit,
                                "evidence_stage": "multi_gpu",
                            }
                        )

        if path_items:
            failure_category = "path_hallucination"
        elif version_items:
            failure_category = "version_hallucination"
        elif capability_items:
            failure_category = "capability_hallucination"
        elif failure_category in {"missing_report", "invalid_json"}:
            pass
        else:
            failure_category = "not_applicable"

    hallucinations = {
        "path": {"count": len(path_items), "items": path_items},
        "version": {"count": len(version_items), "items": version_items},
        "capability": {"count": len(capability_items), "items": capability_items},
    }

    has_hallucination = any(h["count"] > 0 for h in hallucinations.values())
    report_ok = bool(reported) and failure_category not in {"missing_report", "invalid_json"}

    status = "success" if report_ok and not has_hallucination else "failure"
    exit_code = 0 if status == "success" else 1

    assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    manifest = repo_root / "benchmark_assets" / "manifest.json"
    if manifest.exists():
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
            a = m.get("assets", m)
            assets = {"dataset": a.get("dataset", assets["dataset"]), "model": a.get("model", assets["model"])}
        except Exception:
            pass

    payload: dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": f"python {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": assets,
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_timestamp(),
            "env_vars": {k: os.environ.get(k, "") for k in sorted(os.environ) if k.startswith("SCIMLOPSBENCH_")},
        },
        "failure_category": failure_category if exit_code != 0 else "not_applicable",
        "error_excerpt": "" if exit_code == 0 else _tail(log_path),
    }

    _write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
