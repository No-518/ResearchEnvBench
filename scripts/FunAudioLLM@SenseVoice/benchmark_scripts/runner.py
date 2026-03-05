#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text_safely(path: Path, max_bytes: int = 2_000_000) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    except Exception:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return data.decode("utf-8", errors="replace")


def _tail_lines(text: str, max_lines: int = 220) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines).strip()
    return "\n".join(lines[-max_lines:]).strip()


def _load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except Exception as e:
        return None, f"failed to read file {path}: {e}"
    try:
        parsed = json.loads(raw)
    except Exception as e:
        return None, f"invalid json in {path}: {e}"
    if not isinstance(parsed, dict):
        return None, f"json root must be an object in {path}"
    return parsed, None


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:
        return "unknown"


def _select_env_vars(env: Dict[str, str]) -> Dict[str, str]:
    keep_keys = {
        "HOME",
        "PATH",
        "CUDA_VISIBLE_DEVICES",
        "SENSEVOICE_DEVICE",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "XDG_CACHE_HOME",
        "HF_HOME",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "HF_DATASETS_CACHE",
        "TORCH_HOME",
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
    }
    keep_prefixes = (
        "NCCL_",
        "MASTER_",
        "WORLD_SIZE",
        "RANK",
        "LOCAL_RANK",
        "SLURM_",
        "OMP_",
        "MKL_",
        "OPENBLAS_",
        "CUDA_",
        "ROCM_",
        "HIP_",
        "HSA_",
        "TF_",
        "JAX_",
        "XLA_",
        "PYTORCH_",
    )
    selected: Dict[str, str] = {}
    for k, v in env.items():
        if k in keep_keys:
            selected[k] = v
            continue
        if any(k.startswith(p) for p in keep_prefixes):
            selected[k] = v
    return selected


@dataclass(frozen=True)
class PythonResolution:
    python: str
    source: str
    report_path: str
    warning: Optional[str]


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(str(path), os.X_OK)


def resolve_python(
    *,
    cli_python: Optional[str],
    report_path: Path,
    requires_python: bool,
) -> Tuple[Optional[PythonResolution], Optional[str]]:
    if cli_python:
        return PythonResolution(
            python=cli_python,
            source="cli",
            report_path=str(report_path),
            warning=None,
        ), None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return PythonResolution(
            python=env_python,
            source="env",
            report_path=str(report_path),
            warning=None,
        ), None

    report, err = _load_json(report_path)
    if err:
        if requires_python:
            return None, "missing_report"
        fallback = shutil.which("python") or shutil.which("python3") or sys.executable
        return PythonResolution(
            python=fallback,
            source="path_fallback",
            report_path=str(report_path),
            warning=f"report unreadable ({err}); using fallback python from PATH",
        ), None

    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        if requires_python:
            return None, "missing_report"
        fallback = shutil.which("python") or shutil.which("python3") or sys.executable
        return PythonResolution(
            python=fallback,
            source="path_fallback",
            report_path=str(report_path),
            warning="report missing python_path; using fallback python from PATH",
        ), None

    candidate = Path(python_path)
    if _is_executable_file(candidate):
        return PythonResolution(
            python=str(candidate),
            source="report",
            report_path=str(report_path),
            warning=None,
        ), None

    fallback = shutil.which("python") or shutil.which("python3") or sys.executable
    return PythonResolution(
        python=fallback,
        source="path_fallback",
        report_path=str(report_path),
        warning=f"report python_path is not executable: {python_path}; using fallback python from PATH",
    ), None


def _python_version(python_exe: str) -> str:
    try:
        out = subprocess.check_output(
            [python_exe, "-c", "import platform; print(platform.python_version())"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:
        return "unknown"


def _format_command(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def _default_assets() -> Dict[str, Any]:
    return {
        "dataset": {"path": "unknown", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        "model": {"path": "unknown", "source": "unknown", "version": "unknown", "sha256": "unknown"},
    }


def _inherit_assets_from_prepare(repo_root: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    prepare_results = repo_root / "build_output" / "prepare" / "results.json"
    data, err = _load_json(prepare_results)
    if err or not data:
        return _default_assets(), f"prepare results not available: {err}"
    assets = data.get("assets")
    if not isinstance(assets, dict):
        return _default_assets(), "prepare results has no assets object"
    dataset = assets.get("dataset")
    model = assets.get("model")
    if not isinstance(dataset, dict) or not isinstance(model, dict):
        return _default_assets(), "prepare assets missing dataset/model objects"
    return {"dataset": dataset, "model": model}, None


def write_results(path: Path, payload: Dict[str, Any]) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _merge_extra_results(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for k, v in extra.items():
        if k == "meta" and isinstance(v, dict) and isinstance(merged.get("meta"), dict):
            merged["meta"] = {**merged["meta"], **v}
            continue
        if k == "assets" and isinstance(v, dict):
            merged["assets"] = v
            continue
        merged[k] = v
    return merged


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark executor")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    parser.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    parser.add_argument("--timeout-sec", type=int, required=True)
    parser.add_argument("--out-dir", default=None, help="Default: build_output/<stage>")
    parser.add_argument("--requires-python", action="store_true")
    parser.add_argument("--python", dest="cli_python", default=None, help="Override resolved python")
    parser.add_argument("--report-path", default=None, help="Default: /opt/scimlopsbench/report.json")
    parser.add_argument("--skip", action="store_true")
    parser.add_argument(
        "--skip-reason",
        default="not_applicable",
        choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"],
    )
    parser.add_argument("--decision-reason", default="", help="Human-readable selection rationale")
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Extra env var for subprocess, format KEY=VALUE (repeatable)",
    )
    parser.add_argument(
        "--no-inherit-assets",
        action="store_true",
        help="Do not auto-read build_output/prepare/results.json",
    )
    parser.add_argument(
        "--assets-json",
        default=None,
        help="Path to JSON file containing an assets object with dataset/model entries",
    )
    parser.add_argument(
        "--extra-json",
        default=None,
        help="Path to JSON file containing additional top-level fields to merge into results.json",
    )
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute (prefix with --)")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else (REPO_ROOT / "build_output" / args.stage)
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = Path(args.report_path) if args.report_path else Path(os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH))

    assets: Dict[str, Any]
    assets_note: Optional[str] = None
    if args.assets_json:
        assets_data, assets_err = _load_json(Path(args.assets_json))
        if assets_err or not assets_data:
            assets = _default_assets()
            assets_note = f"failed to load assets json ({args.assets_json}): {assets_err}"
        else:
            assets_obj = assets_data.get("assets") if "assets" in assets_data else assets_data
            if isinstance(assets_obj, dict) and "dataset" in assets_obj and "model" in assets_obj:
                assets = assets_obj  # type: ignore[assignment]
            else:
                assets = _default_assets()
                assets_note = f"assets json missing dataset/model objects ({args.assets_json})"
    elif args.no_inherit_assets or args.stage == "prepare":
        assets = _default_assets()
    else:
        assets, assets_note = _inherit_assets_from_prepare(REPO_ROOT)

    python_resolution: Optional[PythonResolution] = None
    python_resolution_err: Optional[str] = None
    if args.requires_python:
        python_resolution, python_resolution_err = resolve_python(
            cli_python=args.cli_python,
            report_path=report_path,
            requires_python=True,
        )
        if python_resolution_err == "missing_report" and python_resolution is None:
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": args.stage,
                "task": args.task,
                "command": "",
                "timeout_sec": args.timeout_sec,
                "framework": args.framework,
                "assets": assets,
                "meta": {
                    "python": "unknown",
                    "git_commit": _git_commit(REPO_ROOT),
                    "env_vars": _select_env_vars(dict(os.environ)),
                    "decision_reason": args.decision_reason,
                    "timestamp_utc": _utc_timestamp(),
                    "report_path": str(report_path),
                    "assets_note": assets_note or "",
                },
                "failure_category": "missing_report",
                "error_excerpt": "Missing or invalid report.json; provide --python or ensure /opt/scimlopsbench/report.json is present.",
            }
            write_results(results_path, payload)
            log_path.write_text(payload["error_excerpt"] + "\n", encoding="utf-8")
            return 1

    if args.skip:
        payload = {
            "status": "skipped",
            "skip_reason": args.skip_reason,
            "exit_code": 0,
            "stage": args.stage,
            "task": args.task,
            "command": "",
            "timeout_sec": args.timeout_sec,
            "framework": args.framework,
            "assets": assets,
            "meta": {
                "python": _python_version(python_resolution.python) if python_resolution else "unknown",
                "git_commit": _git_commit(REPO_ROOT),
                "env_vars": _select_env_vars(dict(os.environ)),
                "decision_reason": args.decision_reason,
                "timestamp_utc": _utc_timestamp(),
                "python_resolution": python_resolution.__dict__ if python_resolution else None,
                "assets_note": assets_note or "",
            },
            "failure_category": "unknown",
            "error_excerpt": "",
        }
        write_results(results_path, payload)
        log_path.write_text("skipped\n", encoding="utf-8")
        return 0

    if not args.cmd:
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": args.stage,
            "task": args.task,
            "command": "",
            "timeout_sec": args.timeout_sec,
            "framework": args.framework,
            "assets": assets,
            "meta": {
                "python": _python_version(python_resolution.python) if python_resolution else "unknown",
                "git_commit": _git_commit(REPO_ROOT),
                "env_vars": _select_env_vars(dict(os.environ)),
                "decision_reason": args.decision_reason,
                "timestamp_utc": _utc_timestamp(),
                "python_resolution": python_resolution.__dict__ if python_resolution else None,
                "assets_note": assets_note or "",
            },
            "failure_category": "args_unknown",
            "error_excerpt": "No command provided to runner.py (missing -- <cmd...>).",
        }
        write_results(results_path, payload)
        log_path.write_text(payload["error_excerpt"] + "\n", encoding="utf-8")
        return 1

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    env = dict(os.environ)
    if python_resolution:
        env["BENCH_PYTHON"] = python_resolution.python

    for item in args.env:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        env[k] = v

    start = time.time()
    failure_category = "unknown"
    command_str = _format_command(cmd)

    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log_f:
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=log_f,
                stderr=log_f,
                text=True,
                start_new_session=True,
            )
            try:
                return_code = proc.wait(timeout=args.timeout_sec)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, 9)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                raise
        if return_code == 0:
            status = "success"
            exit_code = 0
            failure_category = "unknown"
        else:
            status = "failure"
            exit_code = 1
            failure_category = "runtime"
    except FileNotFoundError:
        status = "failure"
        exit_code = 1
        return_code = 127
        failure_category = "entrypoint_not_found"
        log_path.write_text(f"Command not found: {command_str}\n", encoding="utf-8")
    except subprocess.TimeoutExpired:
        status = "failure"
        exit_code = 1
        return_code = 124
        failure_category = "timeout"
        existing = _read_text_safely(log_path)
        log_path.write_text(existing + "\n[runner] TIMEOUT\n", encoding="utf-8")
    except Exception as e:
        status = "failure"
        exit_code = 1
        return_code = 1
        failure_category = "unknown"
        log_path.write_text(f"[runner] unexpected exception: {e}\n", encoding="utf-8")

    elapsed = time.time() - start
    log_text = _read_text_safely(log_path)
    error_excerpt = "" if status == "success" else _tail_lines(log_text, max_lines=240)

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": args.stage,
        "task": args.task,
        "command": command_str,
        "timeout_sec": args.timeout_sec,
        "framework": args.framework,
        "assets": assets,
        "meta": {
            "python": _python_version(python_resolution.python) if python_resolution else "unknown",
            "git_commit": _git_commit(REPO_ROOT),
            "env_vars": _select_env_vars(env),
            "decision_reason": args.decision_reason,
            "timestamp_utc": _utc_timestamp(),
            "elapsed_sec": round(elapsed, 3),
            "return_code": return_code,
            "python_resolution": python_resolution.__dict__ if python_resolution else None,
            "assets_note": assets_note or "",
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    if python_resolution and python_resolution.warning:
        payload["meta"]["python_resolution_warning"] = python_resolution.warning

    extra_note = None
    if args.extra_json:
        extra_data, extra_err = _load_json(Path(args.extra_json))
        if extra_err:
            extra_note = f"failed to load extra json ({args.extra_json}): {extra_err}"
        elif extra_data:
            payload = _merge_extra_results(payload, extra_data)
    if extra_note:
        payload.setdefault("meta", {})
        if isinstance(payload["meta"], dict):
            payload["meta"]["extra_json_note"] = extra_note

    write_results(results_path, payload)
    return 0 if status != "failure" else 1


if __name__ == "__main__":
    raise SystemExit(main())
