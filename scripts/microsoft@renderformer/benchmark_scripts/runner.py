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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        text = _read_text(path)
    except Exception:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[-max_lines:])


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


def _safe_json_load(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(_read_text(path)), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except Exception as e:
        return None, f"invalid json: {path}: {e}"


def _git_commit(repo_root: Path) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode != 0:
            return None
        return res.stdout.strip() or None
    except Exception:
        return None


def _default_timeout_for_stage(stage: str) -> int:
    defaults = {
        "prepare": 1200,
        "cpu": 600,
        "cuda": 120,
        "single_gpu": 600,
        "multi_gpu": 1200,
        "env_size": 120,
        "hallucination": 120,
        "pyright": 600,
        "summary": 120,
    }
    return defaults.get(stage, 600)


def _resolve_report_path(cli: Optional[str]) -> str:
    if cli:
        return cli
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return os.environ["SCIMLOPSBENCH_REPORT"]
    return DEFAULT_REPORT_PATH


def _resolve_python(
    cli_python: Optional[str],
    report_path: str,
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    meta: Dict[str, Any] = {
        "python_resolution": {
            "cli_python": cli_python or "",
            "env_SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            "report_path": report_path,
        }
    }

    if cli_python:
        meta["python_resolution"]["chosen"] = "cli"
        return cli_python, meta, None

    if os.environ.get("SCIMLOPSBENCH_PYTHON"):
        meta["python_resolution"]["chosen"] = "env"
        return os.environ["SCIMLOPSBENCH_PYTHON"], meta, None

    report_data, report_err = _safe_json_load(Path(report_path))
    if report_data is None:
        meta["python_resolution"]["chosen"] = ""
        meta["python_resolution"]["error"] = report_err or "missing report"
        return None, meta, "missing_report"

    python_path = report_data.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        meta["python_resolution"]["chosen"] = ""
        meta["python_resolution"]["error"] = "python_path missing in report"
        return None, meta, "missing_report"

    meta["python_resolution"]["chosen"] = "report"
    return python_path, meta, None


def _is_executable_file(path: str) -> bool:
    try:
        p = Path(path)
        return p.is_file() and os.access(str(p), os.X_OK)
    except Exception:
        return False


def _select_env(env: Dict[str, str], keys: Iterable[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k in keys:
        if k in env:
            out[k] = env[k]
    return out


def _format_cmd(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _parse_success_exit_codes(csv_text: str) -> List[int]:
    values: List[int] = []
    for part in csv_text.split(","):
        part = part.strip()
        if not part:
            continue
        values.append(int(part))
    return values or [0]


def _run_subprocess(
    cmd: Sequence[str],
    cwd: Path,
    env: Dict[str, str],
    timeout_sec: int,
    log_path: Path,
) -> Tuple[int, Optional[str]]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_f:
        try:
            proc = subprocess.Popen(
                list(cmd),
                cwd=str(cwd),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
                preexec_fn=os.setsid if os.name == "posix" else None,
            )
            try:
                returncode = proc.wait(timeout=timeout_sec)
                return returncode, None
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "posix":
                        os.killpg(proc.pid, 9)
                    else:
                        proc.kill()
                except Exception:
                    pass
                return 1, "timeout"
        except FileNotFoundError as e:
            log_f.write(f"\n[runner] FileNotFoundError: {e}\n")
            return 1, "file_not_found"
        except Exception as e:
            log_f.write(f"\n[runner] Exception: {e}\n")
            return 1, "exception"


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified executor for benchmark stages.")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--framework", default="unknown")
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--python", default=None, help="Explicit python executable (highest priority).")
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--assets-from", default=None, help="Path to prepare stage results.json (to copy assets).")
    parser.add_argument("--decision-reason", default="")
    parser.add_argument("--env", action="append", default=[], help="Extra env KEY=VALUE (repeatable).")
    parser.add_argument("--success-exit-codes", default="0", help="Comma-separated list, default: 0")
    parser.add_argument("--skip", action="store_true")
    parser.add_argument("--skip-reason", default="unknown")
    parser.add_argument("--command", default="", help="Command string to record when --skip is used.")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute (prefix with --).")
    args = parser.parse_args()

    repo_root = _repo_root()
    stage = args.stage
    out_dir = Path(args.out_dir) if args.out_dir else repo_root / "build_output" / stage
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = args.timeout_sec if args.timeout_sec is not None else _default_timeout_for_stage(stage)
    success_exit_codes = _parse_success_exit_codes(args.success_exit_codes)

    report_path = _resolve_report_path(args.report_path)
    resolved_python, py_meta, py_failure = _resolve_python(args.python, report_path)

    assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    if args.assets_from:
        data, _err = _safe_json_load(Path(args.assets_from))
        if isinstance(data, dict) and isinstance(data.get("assets"), dict):
            assets = data["assets"]

    env = dict(os.environ)
    for item in args.env:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        env[k] = v

    meta_env_keys = [
        "SCIMLOPSBENCH_PYTHON",
        "SCIMLOPSBENCH_REPORT",
        "CUDA_VISIBLE_DEVICES",
        "ATTN_IMPL",
        "HF_HOME",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "PIP_CACHE_DIR",
        "IMAGEIO_USERDIR",
        "XDG_CACHE_HOME",
        "TMPDIR",
    ]
    meta: Dict[str, Any] = {
        "python": resolved_python or "",
        "git_commit": _git_commit(repo_root) or "",
        "env_vars": _select_env(env, meta_env_keys),
        "decision_reason": args.decision_reason,
        "timestamp_utc": _utc_timestamp(),
    }
    meta.update(py_meta)

    def write_results(
        *,
        status: str,
        exit_code: int,
        command: str,
        skip_reason: str,
        failure_category: str,
    ) -> None:
        error_excerpt = _tail_lines(log_path, max_lines=220)
        final_failure_category = failure_category
        if (
            status == "failure"
            and stage in ("cpu", "single_gpu", "multi_gpu")
            and failure_category in ("runtime", "unknown")
        ):
            inferred = _infer_failure_category_from_log(error_excerpt)
            if inferred:
                final_failure_category = inferred
        payload: Dict[str, Any] = {
            "status": status,
            "skip_reason": skip_reason,
            "exit_code": int(exit_code),
            "stage": stage,
            "task": args.task,
            "command": command,
            "timeout_sec": int(timeout_sec),
            "framework": args.framework,
            "assets": assets,
            "meta": meta,
            "failure_category": final_failure_category,
            "error_excerpt": error_excerpt,
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.skip:
        Path(log_path).write_text(f"[runner] skipped: {args.skip_reason}\n", encoding="utf-8")
        write_results(
            status="skipped",
            exit_code=0,
            command=args.command or "",
            skip_reason=args.skip_reason,
            failure_category="unknown",
        )
        return 0

    if py_failure:
        Path(log_path).write_text(
            f"[runner] python resolution failed: {py_failure}; report_path={report_path}\n",
            encoding="utf-8",
        )
        write_results(
            status="failure",
            exit_code=1,
            command="",
            skip_reason="unknown",
            failure_category=py_failure,
        )
        return 1

    if resolved_python and not _is_executable_file(resolved_python):
        Path(log_path).write_text(
            f"[runner] resolved python is not an executable file: {resolved_python}\n",
            encoding="utf-8",
        )
        write_results(
            status="failure",
            exit_code=1,
            command="",
            skip_reason="unknown",
            failure_category="path_hallucination",
        )
        return 1

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        Path(log_path).write_text("[runner] missing command; pass after --\n", encoding="utf-8")
        write_results(
            status="failure",
            exit_code=1,
            command="",
            skip_reason="unknown",
            failure_category="args_unknown",
        )
        return 1

    cmd = [resolved_python if c == "{python}" else c for c in cmd]
    cmd_str = _format_cmd(cmd)

    returncode, run_err = _run_subprocess(cmd, cwd=repo_root, env=env, timeout_sec=timeout_sec, log_path=log_path)
    if run_err == "timeout":
        write_results(
            status="failure",
            exit_code=1,
            command=cmd_str,
            skip_reason="unknown",
            failure_category="timeout",
        )
        return 1
    if run_err in {"file_not_found"}:
        write_results(
            status="failure",
            exit_code=1,
            command=cmd_str,
            skip_reason="unknown",
            failure_category="entrypoint_not_found",
        )
        return 1
    if run_err in {"exception"}:
        write_results(
            status="failure",
            exit_code=1,
            command=cmd_str,
            skip_reason="unknown",
            failure_category="unknown",
        )
        return 1

    status = "success" if returncode in success_exit_codes else "failure"
    failure_category = "unknown" if status == "success" else "runtime"
    exit_code = 0 if status == "success" else 1
    write_results(
        status=status,
        exit_code=exit_code,
        command=cmd_str,
        skip_reason="unknown",
        failure_category=failure_category,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
