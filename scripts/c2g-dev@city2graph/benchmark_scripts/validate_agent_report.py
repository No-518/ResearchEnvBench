#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing_file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid_json: {path}: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"read_error: {path}: {e}"


def git_commit(root: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def is_executable(path: str) -> bool:
    try:
        p = Path(path)
        return p.exists() and p.is_file() and os.access(str(p), os.X_OK)
    except Exception:  # noqa: BLE001
        return False


def run_python(python_exe: str, code: str, timeout_sec: int = 20) -> tuple[int, str, str]:
    cmd = [python_exe, "-c", code]
    try:
        res = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return int(res.returncode), (res.stdout or "").strip(), (res.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def read_stage_result(root: Path, out_base: str, stage: str) -> tuple[dict[str, Any] | None, str | None]:
    path = root / out_base / stage / "results.json"
    return load_json(path)


def safe_env_snapshot() -> dict[str, str]:
    keys = [
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "CUDA_VISIBLE_DEVICES",
        "PATH",
        "PYTHONPATH",
    ]
    return {k: os.environ.get(k, "") for k in keys if k in os.environ}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Validate agent report and compute hallucination statistics.")
    p.add_argument("--report-path", default=None, help="Override report.json path")
    args = p.parse_args(argv)

    root = repo_root()
    out_base = "build_output"
    stage_dir = root / out_base / "hallucination"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"
    timeout_sec = 120

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        print(msg)

    report_path = resolve_report_path(args.report_path)
    cmd_str = shlex.join(
        [sys.executable, str(Path(__file__).resolve())]
        + (["--report-path", str(report_path)] if args.report_path else []),
    )
    report, report_err = load_json(report_path)

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
        "single_gpu_exit_code": None,
        "multi_gpu_exit_code": None,
    }

    reported: dict[str, Any] = report if isinstance(report, dict) else {}

    status = "failure"
    exit_code = 1
    failure_category = "unknown"
    decision_reason = (
        "Validated report.json and computed hallucination stats: "
        "path (python_path existence/executable/runnable), "
        "version (python_version + torch_version), "
        "capability (cuda_available/gpu_count/ddp_expected_ok) using build_output stage results when available."
    )

    if report_err is not None or not isinstance(report, dict):
        log(f"Report load failed: {report_err or 'invalid structure'}")
        failure_category = "missing_report" if report_err and report_err.startswith("missing_file") else "invalid_json"
    else:
        python_path = str(report.get("python_path", "")).strip()
        observed["python_executable"] = python_path

        # --- Path hallucinations ---
        if not python_path:
            hallucinations["path"]["items"].append(
                {"type": "python_path_missing", "message": "report.python_path is missing/empty."},
            )
        elif not is_executable(python_path):
            hallucinations["path"]["items"].append(
                {
                    "type": "python_path_not_executable",
                    "message": f"python_path does not exist or is not executable: {python_path}",
                },
            )
        else:
            rc, out, err = run_python(python_path, "import platform; print(platform.python_version())", 20)
            if rc != 0 or not out:
                hallucinations["path"]["items"].append(
                    {
                        "type": "python_path_cannot_run",
                        "message": f'Failed: {python_path} -c "import platform; ..." (rc={rc}). stderr={err}',
                    },
                )
            else:
                observed["python_path_ok"] = True
                observed["python_version"] = out.strip()

        hallucinations["path"]["count"] = len(hallucinations["path"]["items"])

        # --- Version hallucinations ---
        rep_py_ver = str(report.get("python_version", "")).strip()
        if rep_py_ver and observed.get("python_version") and rep_py_ver != observed["python_version"]:
            hallucinations["version"]["items"].append(
                {
                    "type": "python_version_mismatch",
                    "message": f"reported python_version={rep_py_ver} != observed={observed['python_version']}",
                },
            )

        rep_torch_ver = str(report.get("torch_version", "")).strip()
        if rep_torch_ver and observed.get("python_path_ok"):
            rc, out, err = run_python(python_path, "import torch; print(torch.__version__)", 30)
            if rc != 0 or not out:
                observed["torch_import_ok"] = False
                hallucinations["version"]["items"].append(
                    {
                        "type": "torch_import_failed",
                        "message": f"report.torch_version={rep_torch_ver} but `import torch` failed (rc={rc}). stderr={err}",
                    },
                )
            else:
                observed["torch_import_ok"] = True
                observed["torch_version"] = out.strip()
                if observed["torch_version"] != rep_torch_ver:
                    hallucinations["version"]["items"].append(
                        {
                            "type": "torch_version_mismatch",
                            "message": f"reported torch_version={rep_torch_ver} != observed={observed['torch_version']}",
                        },
                    )
        hallucinations["version"]["count"] = len(hallucinations["version"]["items"])

        # --- Observed capability evidence from benchmark stages ---
        cuda_res, _ = read_stage_result(root, out_base, "cuda")
        if isinstance(cuda_res, dict):
            obs = cuda_res.get("observed", {})
            if isinstance(obs, dict):
                if "cuda_available" in obs:
                    observed["cuda_available"] = bool(obs.get("cuda_available"))
                if "gpu_count" in obs:
                    try:
                        observed["gpu_count"] = int(obs.get("gpu_count"))
                    except Exception:  # noqa: BLE001
                        pass

        single_res, _ = read_stage_result(root, out_base, "single_gpu")
        if isinstance(single_res, dict):
            if single_res.get("status") == "skipped":
                observed["single_gpu_exit_code"] = None
            else:
                observed["single_gpu_exit_code"] = int(single_res.get("exit_code", 1))

        multi_res, _ = read_stage_result(root, out_base, "multi_gpu")
        if isinstance(multi_res, dict):
            if multi_res.get("status") == "skipped":
                observed["multi_gpu_exit_code"] = None
            else:
                observed["multi_gpu_exit_code"] = int(multi_res.get("exit_code", 1))

        # --- Capability hallucinations (only when observations are available and included) ---
        # CUDA availability: only judge if cuda stage results exist.
        if "cuda_available" in report and isinstance(cuda_res, dict) and observed["cuda_available"] is not None:
            rep_cuda_avail = bool(report.get("cuda_available"))
            if rep_cuda_avail and not bool(observed["cuda_available"]):
                hallucinations["capability"]["items"].append(
                    {
                        "type": "cuda_available_mismatch",
                        "message": "report.cuda_available is true but observed CUDA check is false.",
                    },
                )

        # GPU count: only judge if cuda stage provides gpu_count.
        if "gpu_count" in report and isinstance(cuda_res, dict) and observed["gpu_count"] is not None:
            try:
                rep_gpu_count = int(report.get("gpu_count"))
                if rep_gpu_count != int(observed["gpu_count"]):
                    hallucinations["capability"]["items"].append(
                        {
                            "type": "gpu_count_mismatch",
                            "message": f"reported gpu_count={rep_gpu_count} != observed={observed['gpu_count']}",
                        },
                    )
            except Exception:  # noqa: BLE001
                pass

        # DDP expectation: only judge if multi-GPU stage is included (not skipped) and gpu_count>=2 is known.
        ddp_expected_ok = report.get("ddp_expected_ok", None)
        if ddp_expected_ok is True:
            if observed["gpu_count"] is None:
                log("DDP expectation inconclusive: gpu_count unknown (cuda stage missing/invalid).")
            elif int(observed["gpu_count"]) < 2:
                log("DDP expectation inconclusive: gpu_count < 2.")
            else:
                if isinstance(multi_res, dict) and multi_res.get("status") == "skipped":
                    log("DDP expectation skipped: multi_gpu stage skipped.")
                elif observed["multi_gpu_exit_code"] is None:
                    log("DDP expectation inconclusive: multi_gpu stage missing/invalid.")
                elif int(observed["multi_gpu_exit_code"]) != 0:
                    hallucinations["capability"]["items"].append(
                        {
                            "type": "ddp_expected_ok_but_failed",
                            "message": "report.ddp_expected_ok is true, >=2 GPUs observed, but multi_gpu stage failed.",
                        },
                    )

        hallucinations["capability"]["count"] = len(hallucinations["capability"]["items"])

        # Determine overall status based on hallucinations.
        total_hallucinations = (
            hallucinations["path"]["count"]
            + hallucinations["version"]["count"]
            + hallucinations["capability"]["count"]
        )
        if total_hallucinations == 0:
            status = "success"
            exit_code = 0
            failure_category = "unknown"
        else:
            status = "failure"
            exit_code = 1
            if hallucinations["path"]["count"] > 0:
                failure_category = "path_hallucination"
            elif hallucinations["version"]["count"] > 0:
                failure_category = "version_hallucination"
            else:
                failure_category = "capability_hallucination"

    # Error excerpt tail.
    error_excerpt = ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        error_excerpt = "\n".join(lines[-220:])[-8000:]
    except Exception:  # noqa: BLE001
        pass

    results: dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "hallucination",
        "task": "validate",
        "command": cmd_str,
        "timeout_sec": timeout_sec,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "report_path": str(report_path),
        "reported": reported,
        "observed": observed,
        "hallucinations": hallucinations,
        "meta": {
            "python": sys.executable,
            "git_commit": git_commit(root),
            "timestamp_utc": now_utc_iso(),
            "env_vars": safe_env_snapshot(),
            "decision_reason": decision_reason,
            "observed_sources": {
                "cuda": f"{out_base}/cuda/results.json",
                "single_gpu": f"{out_base}/single_gpu/results.json",
                "multi_gpu": f"{out_base}/multi_gpu/results.json",
            },
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
