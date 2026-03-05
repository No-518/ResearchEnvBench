#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_read_text(path: Path, max_bytes: int = 2_000_000) -> str:
    try:
        with path.open("rb") as f:
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _tail_lines(text: str, max_lines: int = 220) -> str:
    lines = text.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail).strip()


def _infer_failure_category_from_log(log_text: str) -> str:
    hay = (log_text or "").lower()
    if not hay:
        return ""
    if "modulenotfounderror" in hay or "no module named" in hay:
        return "deps"
    if "importerror" in hay or "cannot import name" in hay:
        return "deps"
    if "undefined symbol" in hay or "symbol not found" in hay or "libtorch" in hay:
        return "deps"
    if "torch.utils._pytree" in hay or "register_pytree_node" in hay:
        return "deps"
    if "_array_api" in hay or "failed to initialize numpy" in hay:
        return "deps"
    if "cudaexecutionprovider not available" in hay or "onnxruntime-gpu" in hay:
        return "deps"
    if "torchrun: command not found" in hay or "command not found: torchrun" in hay:
        return "deps"
    if "does not seem to have any of the loading methods defined" in hay and "placeholder" in hay:
        return "deps"
    return ""


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
        return out.strip()
    except Exception:
        return ""


def _load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing_json_file:{path}"
    except Exception as e:
        return None, f"invalid_json:{path}:{type(e).__name__}:{e}"


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path("/opt/scimlopsbench/report.json")


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def _which(cmd: str) -> Optional[str]:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(p) / cmd
        if _is_executable_file(cand):
            return str(cand)
    return None


def _resolve_python(
    cli_python: Optional[str],
    requires_python: bool,
    report_path: Path,
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    meta: Dict[str, Any] = {
        "python_resolution": {
            "cli_python": cli_python or "",
            "env_SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            "report_path": str(report_path),
            "report_python_path": "",
            "used_fallback_path_python": False,
            "warning": "",
        }
    }

    if cli_python:
        py = Path(cli_python)
        if _is_executable_file(py):
            return str(py), meta, None
        return None, meta, f"--python is not executable: {cli_python}"

    env_py = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_py:
        py = Path(env_py)
        if _is_executable_file(py):
            return str(py), meta, None
        meta["python_resolution"]["warning"] = f"SCIMLOPSBENCH_PYTHON not executable: {env_py}"

    report_obj, report_err = _load_json(report_path)
    if report_obj is None:
        if requires_python:
            return None, meta, report_err or "missing_report"
        return None, meta, None

    report_python_path = str(report_obj.get("python_path", "") or "")
    meta["python_resolution"]["report_python_path"] = report_python_path
    if report_python_path:
        py = Path(report_python_path)
        if _is_executable_file(py):
            return str(py), meta, None
        meta["python_resolution"]["warning"] = f"report python_path not executable: {report_python_path}"

    fallback = _which("python3") or _which("python")
    if fallback:
        meta["python_resolution"]["used_fallback_path_python"] = True
        if not meta["python_resolution"]["warning"]:
            meta["python_resolution"]["warning"] = "Fell back to python from PATH"
        return fallback, meta, None

    if requires_python:
        return None, meta, "no_python_found"
    return None, meta, None


def _render_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _replace_placeholders(cmd: List[str], python_path: Optional[str]) -> List[str]:
    out: List[str] = []
    for part in cmd:
        if part == "{{python}}":
            if not python_path:
                out.append("python")
            else:
                out.append(python_path)
        else:
            out.append(part)
    return out


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified stage runner for scimlopsbench.")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--framework", default="unknown")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--timeout-sec", type=int, default=600)
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--assets-from-prepare", default="build_output/prepare/results.json")
    parser.add_argument("--report-path", default="")
    parser.add_argument("--python", default="")
    parser.add_argument(
        "--requires-python",
        action="store_true",
        help="Enforce python resolution from report.json / overrides and enable {{python}} placeholder.",
    )
    parser.add_argument(
        "--skip",
        action="store_true",
        help="Skip this stage (writes results.json/log.txt with status=skipped).",
    )
    parser.add_argument("--skip-reason", default="unknown")
    parser.add_argument("--failure-category", default="unknown")
    parser.add_argument("--env", action="append", default=[], help="Extra env var KEY=VALUE (repeatable).")
    parser.add_argument("--cwd", default="", help="Working directory (default: repo root).")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run (prefix with --).")
    args = parser.parse_args()

    repo_root = _repo_root()
    out_dir = Path(args.out_dir) if args.out_dir else (repo_root / "build_output" / args.stage)
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    env = os.environ.copy()
    extra_env: Dict[str, str] = {}
    for item in args.env:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        extra_env[k] = v
        env[k] = v

    report_path = _resolve_report_path(args.report_path or None)
    python_path, py_meta, py_err = _resolve_python(
        args.python or None,
        requires_python=bool(args.requires_python),
        report_path=report_path,
    )

    cmd = [c for c in args.cmd if c != "--"]
    cmd = _replace_placeholders(cmd, python_path)
    cmd_str = _render_cmd(cmd) if cmd else ""

    assets_obj: Dict[str, Any] = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    prepare_path = repo_root / args.assets_from_prepare
    prepare_json, prepare_err = _load_json(prepare_path)
    if prepare_json and isinstance(prepare_json.get("assets"), dict):
        assets_obj = prepare_json["assets"]  # type: ignore[assignment]

    meta: Dict[str, Any] = {
        "python": python_path or "",
        "git_commit": _git_commit(repo_root),
        "env_vars": {
            "CUDA_VISIBLE_DEVICES": env.get("CUDA_VISIBLE_DEVICES", ""),
            "DETECTRON2_DATASETS": env.get("DETECTRON2_DATASETS", ""),
            "SCIMLOPSBENCH_REPORT": str(report_path),
            "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            **extra_env,
        },
        "decision_reason": args.decision_reason,
        "timestamp_utc": _utc_timestamp(),
        **py_meta,
    }

    payload: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": args.stage,
        "task": args.task,
        "command": cmd_str,
        "timeout_sec": int(args.timeout_sec),
        "framework": args.framework,
        "assets": assets_obj,
        "meta": meta,
        "failure_category": args.failure_category,
        "error_excerpt": "",
    }

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"[runner] stage={args.stage} task={args.task}\n")
        logf.write(f"[runner] repo_root={repo_root}\n")
        logf.write(f"[runner] out_dir={out_dir}\n")
        logf.write(f"[runner] cmd={cmd_str}\n")
        logf.write(f"[runner] timeout_sec={args.timeout_sec}\n")
        logf.write(f"[runner] report_path={report_path}\n")
        logf.write(f"[runner] resolved_python={python_path or ''}\n")
        if py_err:
            logf.write(f"[runner] python_resolution_error={py_err}\n")
        logf.flush()

        if args.requires_python and py_err:
            payload["status"] = "failure"
            payload["exit_code"] = 1
            payload["failure_category"] = "missing_report" if "missing_json_file" in py_err or "invalid_json" in py_err else "path_hallucination"
            payload["error_excerpt"] = _tail_lines(f"python resolution failed: {py_err}\n")
            _write_json(results_path, payload)
            return 1

        if args.skip:
            payload["status"] = "skipped"
            payload["skip_reason"] = args.skip_reason or "unknown"
            payload["exit_code"] = 0
            payload["failure_category"] = ""
            payload["error_excerpt"] = ""
            _write_json(results_path, payload)
            logf.write(f"[runner] skipped: {payload['skip_reason']}\n")
            return 0

        if not cmd:
            payload["status"] = "failure"
            payload["exit_code"] = 1
            payload["failure_category"] = "args_unknown"
            payload["error_excerpt"] = _tail_lines("No command provided.\n")
            _write_json(results_path, payload)
            return 1

        workdir = Path(args.cwd).resolve() if args.cwd else repo_root
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workdir),
                env=env,
                stdout=logf,
                stderr=logf,
                text=True,
                timeout=float(args.timeout_sec),
            )
            rc = int(proc.returncode)
            elapsed = time.time() - t0
            payload["meta"]["duration_sec"] = round(elapsed, 3)
            payload["exit_code"] = rc
            if rc == 0:
                payload["status"] = "success"
                payload["skip_reason"] = "not_applicable"
                payload["failure_category"] = ""
            else:
                payload["status"] = "failure"
                if payload["failure_category"] == "unknown":
                    payload["failure_category"] = "runtime"
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            payload["meta"]["duration_sec"] = round(elapsed, 3)
            payload["status"] = "failure"
            payload["exit_code"] = 1
            payload["failure_category"] = "timeout"
        except FileNotFoundError as e:
            payload["status"] = "failure"
            payload["exit_code"] = 1
            payload["failure_category"] = "entrypoint_not_found"
            logf.write(f"[runner] FileNotFoundError: {e}\n")
        except Exception as e:
            payload["status"] = "failure"
            payload["exit_code"] = 1
            if payload["failure_category"] == "unknown":
                payload["failure_category"] = "runtime"
            logf.write(f"[runner] Exception: {type(e).__name__}: {e}\n")

    payload["error_excerpt"] = _tail_lines(_safe_read_text(log_path), max_lines=240)
    if (
        payload["status"] == "failure"
        and stage in ("cpu", "single_gpu", "multi_gpu")
        and payload.get("failure_category") in ("unknown", "runtime")
    ):
        inferred = _infer_failure_category_from_log(payload["error_excerpt"])
        if inferred:
            payload["failure_category"] = inferred
    _write_json(results_path, payload)

    return 0 if payload["status"] in ("success", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
