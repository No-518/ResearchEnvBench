#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _get_git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        text = _read_text(path)
    except Exception:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _report_path(cli_report_path: str | None) -> Path:
    def norm(p: Path) -> Path:
        # Support passing a directory (use <dir>/report.json).
        try:
            if p.is_dir():
                return p / "report.json"
        except Exception:
            pass
        return p

    if cli_report_path:
        return norm(Path(cli_report_path))
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return norm(Path(os.environ["SCIMLOPSBENCH_REPORT"]))
    return norm(Path("/opt/scimlopsbench/report.json"))


def _load_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"Missing JSON file: {path}"
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON in {path}: {e}"
    except Exception as e:
        return None, f"Failed to read {path}: {e}"


@dataclass
class PythonResolution:
    python_path: Optional[str]
    source: str
    warning: Optional[str] = None
    report_path: Optional[str] = None
    report_ok: bool = False


def _is_executable_file(path: str) -> bool:
    p = Path(path)
    return p.exists() and os.access(str(p), os.X_OK) and p.is_file()


def resolve_python(
    *,
    cli_python: str | None,
    report_path: Path,
) -> Tuple[Optional[PythonResolution], Optional[str]]:
    """
    Resolution priority:
      1) CLI --python
      2) Env var SCIMLOPSBENCH_PYTHON
      3) python_path from report.json
      4) Fallback python from PATH (ONLY if report.json is valid but python_path is missing/invalid)

    If report.json is missing/invalid AND no CLI/env override is provided, return failure_category 'missing_report'.
    """
    if cli_python:
        return PythonResolution(cli_python, source="cli", report_path=str(report_path), report_ok=False), None

    if os.environ.get("SCIMLOPSBENCH_PYTHON"):
        return (
            PythonResolution(os.environ["SCIMLOPSBENCH_PYTHON"], source="env:SCIMLOPSBENCH_PYTHON", report_path=str(report_path), report_ok=False),
            None,
        )

    report, err = _load_json(report_path)
    if report is None:
        return None, "missing_report"

    python_path = report.get("python_path") if isinstance(report, dict) else None
    if python_path and isinstance(python_path, str):
        if _is_executable_file(python_path):
            return PythonResolution(python_path, source="report:python_path", report_path=str(report_path), report_ok=True), None
        # Report exists but python_path is not executable: fall back to PATH python and record warning.
        return (
            PythonResolution(
                None,
                source="fallback",
                warning=f"report.python_path is not executable: {python_path!r}; falling back to python from PATH",
                report_path=str(report_path),
                report_ok=True,
            ),
            None,
        )

    # Report exists but missing python_path: fall back to PATH python and record warning.
    return (
        PythonResolution(
            None,
            source="fallback",
            warning="report.json is valid but missing 'python_path'; falling back to python from PATH",
            report_path=str(report_path),
            report_ok=True,
        ),
        None,
    )


def _python_from_path() -> Optional[str]:
    for candidate in ("python3", "python"):
        p = shutil_which(candidate)
        if p:
            return p
    return None


def shutil_which(name: str) -> Optional[str]:
    paths = os.environ.get("PATH", "").split(os.pathsep)
    for p in paths:
        cand = Path(p) / name
        if cand.exists() and os.access(str(cand), os.X_OK) and cand.is_file():
            return str(cand)
    return None


def _get_python_version(python_exe: str) -> str:
    try:
        out = subprocess.check_output(
            [python_exe, "-c", "import platform; print(platform.python_version())"],
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _load_assets_from_prepare(path: Path) -> Tuple[dict, List[str]]:
    warnings: List[str] = []
    data, err = _load_json(path)
    if data is None:
        warnings.append(err or f"Failed to read assets from {path}")
        return (
            {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            warnings,
        )
    assets = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(assets, dict):
        warnings.append(f"Missing/invalid 'assets' in {path}")
        return (
            {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            warnings,
        )
    # Ensure required keys exist.
    dataset = assets.get("dataset") if isinstance(assets.get("dataset"), dict) else {}
    model = assets.get("model") if isinstance(assets.get("model"), dict) else {}
    return (
        {
            "dataset": {
                "path": str(dataset.get("path", "")),
                "source": str(dataset.get("source", "")),
                "version": str(dataset.get("version", "")),
                "sha256": str(dataset.get("sha256", "")),
            },
            "model": {
                "path": str(model.get("path", "")),
                "source": str(model.get("source", "")),
                "version": str(model.get("version", "")),
                "sha256": str(model.get("sha256", "")),
            },
        },
        warnings,
    )


def _categorize_failure(log_tail: str, return_code: Optional[int], timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    import re

    lower = log_tail.lower()
    if "no module named" in lower or "modulenotfounderror" in lower or "importerror" in lower:
        return "deps"
    if "cuda out of memory" in lower or "out of memory" in lower:
        return "oom"
    if "unrecognized arguments" in lower or "unknown argument" in lower:
        return "args_unknown"
    if "file not found" in lower or "filenotfounderror" in lower or "no such file or directory" in lower:
        # Heuristic: prefer model/data based on keywords.
        if "audiovae.pth" in lower or "model.safetensors" in lower or "pytorch_model.bin" in lower or "config.json" in lower:
            return "model"
        if "manifest" in lower or ".jsonl" in lower or "dataset" in lower or "examples/example.wav" in lower:
            return "data"
        return "entrypoint_not_found"

    # Auth / gated-model heuristics.
    #
    # Avoid naive substring checks like `"403" in lower` because the digits can
    # appear in timestamps or other numeric blobs (false positives).
    http_code_match = re.search(r"(^|[^0-9])(401|403)($|[^0-9])", lower)
    if http_code_match and (
        "http" in lower
        or "client error" in lower
        or "status code" in lower
        or "unauthorized" in lower
        or "forbidden" in lower
        or "gated" in lower
        or "access denied" in lower
    ):
        return "auth_required"
    if (
        ("hf_token" in lower or "huggingface_hub_token" in lower or "token" in lower)
        and ("required" in lower or "missing" in lower or "invalid" in lower or "unauthorized" in lower or "forbidden" in lower)
    ):
        return "auth_required"
    if "connection error" in lower or "failed to establish a new connection" in lower or "name or service not known" in lower:
        return "download_failed"
    if return_code is None:
        return "unknown"
    return "runtime"


def _shlex_join(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def write_results(
    *,
    out_dir: Path,
    stage: str,
    task: str,
    status: str,
    skip_reason: str,
    exit_code: int,
    command_str: str,
    timeout_sec: int,
    framework: str,
    assets: dict,
    meta: dict,
    failure_category: str,
    error_excerpt: str,
) -> None:
    _safe_mkdir(out_dir)
    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": int(exit_code),
        "stage": stage,
        "task": task,
        "command": command_str,
        "timeout_sec": int(timeout_sec),
        "framework": framework,
        "assets": assets,
        "meta": meta,
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }
    (out_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark command runner (writes log.txt and results.json).")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True, choices=["train", "infer", "check", "download", "validate", "measure"])
    parser.add_argument("--framework", default="unknown", choices=["pytorch", "tensorflow", "jax", "unknown"])
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--python", dest="cli_python", default=None)
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--assets-from", default="build_output/prepare/results.json")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--status", choices=["success", "failure", "skipped"], default=None)
    parser.add_argument("--skip-reason", default="not_applicable")
    parser.add_argument("--failure-category", default="unknown")
    parser.add_argument("--message", default="")
    parser.add_argument("--command-str", default="")
    parser.add_argument("--no-require-python", action="store_true", help="Allow running python commands without report.json.")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run, preceded by --")
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    stage_default_timeouts = {
        "prepare": 1200,
        "cpu": 600,
        "single_gpu": 600,
        "multi_gpu": 1200,
        "env_size": 120,
        "hallucination": 120,
    }
    timeout_sec = int(args.timeout_sec) if args.timeout_sec is not None else int(stage_default_timeouts.get(args.stage, 600))
    out_dir = Path(args.out_dir) if args.out_dir else (repo_root / "build_output" / args.stage)
    log_path = out_dir / "log.txt"
    _safe_mkdir(out_dir)

    # Always write something to log.txt, even in write-only mode.
    if args.message:
        log_path.write_text(args.message.strip() + "\n", encoding="utf-8")
    else:
        log_path.write_text("", encoding="utf-8")

    assets_from = repo_root / args.assets_from if not os.path.isabs(args.assets_from) else Path(args.assets_from)
    assets, asset_warnings = _load_assets_from_prepare(assets_from)

    report_path = _report_path(args.report_path)
    git_commit = _get_git_commit(repo_root)

    meta: Dict[str, Any] = {
        "python": "",
        "python_version": "",
        "git_commit": git_commit,
        "timestamp_utc": _utc_now_iso(),
        "env_vars": {
            k: os.environ.get(k, "")
            for k in [
                "CUDA_VISIBLE_DEVICES",
                "HF_HOME",
                "HF_HUB_CACHE",
                "HUGGINGFACE_HUB_CACHE",
                "HF_DATASETS_CACHE",
                "TRANSFORMERS_CACHE",
                "TORCH_HOME",
                "XDG_CACHE_HOME",
                "PIP_CACHE_DIR",
                "NCCL_SHM_DISABLE",
                "NCCL_BUFFSIZE",
                "NCCL_P2P_DISABLE",
                "NCCL_IB_DISABLE",
                "SCIMLOPSBENCH_PYTHON",
                "SCIMLOPSBENCH_REPORT",
            ]
        },
        "decision_reason": args.decision_reason,
        "warnings": [w for w in asset_warnings if w],
    }

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    # Write-only mode.
    if args.status is not None and not cmd:
        command_str = args.command_str or ""
        status = args.status
        exit_code = 0 if status in ("success", "skipped") else 1
        write_results(
            out_dir=out_dir,
            stage=args.stage,
            task=args.task,
            status=status,
            skip_reason=args.skip_reason,
            exit_code=exit_code,
            command_str=command_str,
            timeout_sec=timeout_sec,
            framework=args.framework,
            assets=assets,
            meta=meta,
            failure_category=args.failure_category,
            error_excerpt=_tail_lines(log_path),
        )
        return exit_code

    if not cmd:
        # Nothing to run: treat as failure.
        meta["warnings"].append("runner.py invoked without a command")
        write_results(
            out_dir=out_dir,
            stage=args.stage,
            task=args.task,
            status="failure",
            skip_reason=args.skip_reason,
            exit_code=1,
            command_str="",
            timeout_sec=timeout_sec,
            framework=args.framework,
            assets=assets,
            meta=meta,
            failure_category="entrypoint_not_found",
            error_excerpt=_tail_lines(log_path),
        )
        return 1

    needs_python = cmd[0] in ("python", "python3")
    resolved_python: Optional[PythonResolution] = None
    if needs_python:
        resolved_python, python_fail = resolve_python(cli_python=args.cli_python, report_path=report_path)
        if python_fail == "missing_report" and not args.no_require_python:
            log_path.write_text(
                f"ERROR: missing or invalid report.json at {report_path}. Provide --python or set SCIMLOPSBENCH_PYTHON.\n",
                encoding="utf-8",
            )
            meta["python"] = ""
            meta["report_path"] = str(report_path)
            meta["report_ok"] = False
            write_results(
                out_dir=out_dir,
                stage=args.stage,
                task=args.task,
                status="failure",
                skip_reason=args.skip_reason,
                exit_code=1,
                command_str=_shlex_join(cmd),
                timeout_sec=timeout_sec,
                framework=args.framework,
                assets=assets,
                meta=meta,
                failure_category="missing_report",
                error_excerpt=_tail_lines(log_path),
            )
            return 1

        python_exe = resolved_python.python_path if resolved_python and resolved_python.python_path else _python_from_path()
        if not python_exe:
            log_path.write_text("ERROR: could not find python in PATH.\n", encoding="utf-8")
            write_results(
                out_dir=out_dir,
                stage=args.stage,
                task=args.task,
                status="failure",
                skip_reason=args.skip_reason,
                exit_code=1,
                command_str=_shlex_join(cmd),
                timeout_sec=timeout_sec,
                framework=args.framework,
                assets=assets,
                meta=meta,
                failure_category="deps",
                error_excerpt=_tail_lines(log_path),
            )
            return 1

        if resolved_python and resolved_python.warning:
            meta["warnings"].append(resolved_python.warning)

        meta["python"] = python_exe
        meta["python_version"] = _get_python_version(python_exe)
        meta["report_path"] = resolved_python.report_path if resolved_python else str(report_path)
        meta["report_ok"] = bool(resolved_python.report_ok) if resolved_python else False

        cmd = [python_exe, *cmd[1:]]
    else:
        # No python resolution needed for non-python commands.
        meta["python"] = ""
        meta["python_version"] = ""

    command_str = _shlex_join(cmd)
    start = time.time()
    timed_out = False
    return_code: Optional[int] = None

    run_env = dict(os.environ)
    run_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    run_env.setdefault("PYTHONUNBUFFERED", "1")

    try:
        with log_path.open("ab") as f:
            f.write(f"[runner] cwd={repo_root}\n".encode("utf-8", errors="replace"))
            f.write(f"[runner] command={command_str}\n".encode("utf-8", errors="replace"))
            f.flush()
            completed = subprocess.run(
                cmd,
                cwd=str(repo_root),
                env=run_env,
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=timeout_sec,
                check=False,
            )
            return_code = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        return_code = None
        with log_path.open("ab") as f:
            f.write(b"[runner] ERROR: timed out\n")
    except FileNotFoundError as e:
        return_code = None
        with log_path.open("ab") as f:
            f.write(f"[runner] ERROR: {e}\n".encode("utf-8", errors="replace"))
    except Exception as e:
        return_code = None
        with log_path.open("ab") as f:
            f.write(f"[runner] ERROR: {e}\n".encode("utf-8", errors="replace"))

    duration = time.time() - start
    meta["duration_sec"] = round(duration, 3)
    meta["command_exit_code"] = return_code

    log_tail = _tail_lines(log_path)
    if timed_out:
        status = "failure"
        exit_code = 1
        failure_category = "timeout"
    elif return_code == 0:
        status = "success"
        exit_code = 0
        failure_category = "unknown"
    else:
        status = "failure"
        exit_code = 1
        failure_category = _categorize_failure(log_tail, return_code, timed_out)

    write_results(
        out_dir=out_dir,
        stage=args.stage,
        task=args.task,
        status=status,
        skip_reason=args.skip_reason if status == "skipped" else "not_applicable",
        exit_code=exit_code,
        command_str=command_str,
        timeout_sec=timeout_sec,
        framework=args.framework,
        assets=assets,
        meta=meta,
        failure_category=failure_category if status == "failure" else "unknown",
        error_excerpt=log_tail,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
