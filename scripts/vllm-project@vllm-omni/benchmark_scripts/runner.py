#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"

DEFAULT_TIMEOUT_BY_STAGE_SEC: dict[str, int] = {
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


def _now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def _safe_mkdir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _tail_text(path: pathlib.Path, *, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = txt.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)


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


def _git_commit(repo: pathlib.Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, stderr=subprocess.DEVNULL, text=True)
        return out.strip()
    except Exception:
        return ""


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: pathlib.Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _is_executable_file(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except Exception:
        return False


def _default_env_snapshot() -> dict[str, str]:
    keys = [
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "CUDA_VISIBLE_DEVICES",
        "ASCEND_RT_VISIBLE_DEVICES",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "DIFFUSERS_CACHE",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
        "PYTHONPATH",
        "VLLM_USE_MODELSCOPE",
    ]
    out: dict[str, str] = {}
    for k in keys:
        if k in os.environ:
            out[k] = os.environ[k]
    return out


def resolve_report_path(cli_report_path: str | None) -> pathlib.Path:
    if cli_report_path:
        return pathlib.Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return pathlib.Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return pathlib.Path(DEFAULT_REPORT_PATH)


@dataclass(frozen=True)
class ResolvedPython:
    path: str
    source: str  # "cli" | "env" | "report" | "path_fallback"
    warning: str | None = None


def resolve_python(
    *,
    cli_python: str | None,
    report_path: pathlib.Path,
    python_required: bool,
) -> tuple[ResolvedPython | None, str | None, str | None]:
    """Return (resolved_python, failure_category, error_message)."""
    if cli_python:
        return ResolvedPython(path=cli_python, source="cli"), None, None

    env_python = os.environ.get("SCIMLOPSBENCH_PYTHON")
    if env_python:
        return ResolvedPython(path=env_python, source="env"), None, None

    report: dict[str, Any] | None = None
    if report_path.exists():
        try:
            report = _load_json(report_path)
        except Exception as e:
            if python_required:
                return None, "missing_report", f"Failed to parse report JSON at {report_path}: {e}"
            report = None
    else:
        if python_required:
            return None, "missing_report", f"Report file not found at {report_path}"

    if report is not None:
        python_path = report.get("python_path")
        if isinstance(python_path, str) and python_path:
            if _is_executable_file(python_path):
                return ResolvedPython(path=python_path, source="report"), None, None
            # Report is present but path is wrong: allow fallback, but record warning.
            fallback = shutil.which("python3") or shutil.which("python") or "python"
            warning = f"python_path in report is not executable: {python_path!r}; falling back to {fallback!r}"
            return ResolvedPython(path=fallback, source="path_fallback", warning=warning), None, None

        # Report present but missing python_path: allow fallback (path hallucination is handled later).
        fallback = shutil.which("python3") or shutil.which("python") or "python"
        warning = f"python_path missing in report; falling back to {fallback!r}"
        return ResolvedPython(path=fallback, source="path_fallback", warning=warning), None, None

    # No report and python not required: use PATH python.
    fallback = shutil.which("python3") or shutil.which("python") or "python"
    return ResolvedPython(path=fallback, source="path_fallback", warning="Using PATH python (no report)."), None, None


def _format_command_for_results(argv: list[str]) -> str:
    def _quote(s: str) -> str:
        if s == "":
            return "''"
        if any(ch.isspace() or ch in "\"'\\$`" for ch in s):
            return "'" + s.replace("'", "'\"'\"'") + "'"
        return s

    return " ".join(_quote(a) for a in argv)


def _coerce_assets(assets: Any) -> dict[str, Any]:
    if isinstance(assets, dict):
        return assets
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _assets_from_prepare(prepare_results_path: pathlib.Path) -> dict[str, Any] | None:
    if not prepare_results_path.exists():
        return None
    try:
        data = _load_json(prepare_results_path)
        assets = data.get("assets")
        if isinstance(assets, dict):
            return assets
    except Exception:
        return None
    return None


def main(argv: list[str]) -> int:
    repo = _repo_root()

    ap = argparse.ArgumentParser(description="Unified benchmark command runner.")
    ap.add_argument("--stage", required=True, help="Stage name (e.g., cpu, single_gpu).")
    ap.add_argument("--task", required=True, help="Task label (train|infer|check|download|validate|measure).")
    ap.add_argument("--framework", default="unknown", help="Framework (pytorch|tensorflow|jax|unknown).")
    ap.add_argument("--out-dir", default=None, help="Output directory (default: build_output/<stage>).")
    ap.add_argument("--timeout-sec", type=int, default=None, help="Timeout seconds (default by stage).")
    ap.add_argument("--report-path", default=None, help="Override report path (default: /opt/scimlopsbench/report.json).")
    ap.add_argument("--python", default=None, help="Explicit python executable to use (highest priority).")
    ap.add_argument("--python-required", action="store_true", help="Fail if report missing/invalid and no --python.")
    ap.add_argument(
        "--prepare-results",
        default="build_output/prepare/results.json",
        help="Path to prepare stage results.json (for assets propagation).",
    )
    ap.add_argument("--decision-reason", default="", help="Why this command/params were chosen.")
    ap.add_argument(
        "--skip",
        action="store_true",
        help="Skip execution (writes results.json with status=skipped, exit 0).",
    )
    ap.add_argument(
        "--skip-reason",
        default="unknown",
        help="Skip reason (repo_not_supported|insufficient_hardware|not_applicable|unknown).",
    )
    ap.add_argument("--failure-category", default="", help="Override failure_category on failure.")
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run (use: ... -- <command...>).")

    args = ap.parse_args(argv)

    stage = args.stage
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else (repo / "build_output" / stage)
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = args.timeout_sec
    if timeout_sec is None:
        timeout_sec = DEFAULT_TIMEOUT_BY_STAGE_SEC.get(stage, 600)

    report_path = resolve_report_path(args.report_path)

    resolved_py, py_fail_category, py_err = resolve_python(
        cli_python=args.python,
        report_path=report_path,
        python_required=bool(args.python_required),
    )

    # Always create a log file.
    with log_path.open("w", encoding="utf-8") as log_fp:
        log_fp.write(f"[runner] stage={stage} task={args.task} time_utc={_now_utc_iso()}\n")
        log_fp.write(f"[runner] repo_root={repo}\n")
        log_fp.write(f"[runner] report_path={report_path}\n")
        if resolved_py:
            log_fp.write(f"[runner] python={resolved_py.path} (source={resolved_py.source})\n")
            if resolved_py.warning:
                log_fp.write(f"[runner] python_warning={resolved_py.warning}\n")
        if args.skip:
            log_fp.write(f"[runner] skipped: {args.skip_reason}\n")

    base_assets = _assets_from_prepare(repo / args.prepare_results)
    assets = _coerce_assets(base_assets)

    result: dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": args.task,
        "command": "",
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": assets,
        "meta": {
            "python": resolved_py.path if resolved_py else "",
            "python_source": resolved_py.source if resolved_py else "",
            "python_warning": resolved_py.warning if (resolved_py and resolved_py.warning) else "",
            "git_commit": _git_commit(repo),
            "env_vars": _default_env_snapshot(),
            "decision_reason": args.decision_reason,
            "timestamp_utc": _now_utc_iso(),
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    if args.skip:
        result["status"] = "skipped"
        result["skip_reason"] = args.skip_reason
        result["exit_code"] = 0
        result["failure_category"] = ""
        result["command"] = ""
        result["error_excerpt"] = ""
        _write_json(results_path, result)
        return 0

    if py_fail_category:
        result["status"] = "failure"
        result["failure_category"] = py_fail_category
        result["exit_code"] = 1
        result["command"] = ""
        result["error_excerpt"] = py_err or ""
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(f"[runner] python_resolution_failed: {py_err}\n")
        _write_json(results_path, result)
        return 1

    if not args.cmd:
        result["failure_category"] = "args_unknown"
        result["error_excerpt"] = "No command provided. Use: runner.py ... -- <command...>"
        _write_json(results_path, result)
        return 1

    cmd_tokens = list(args.cmd)
    # argparse.REMAINDER may include one or more literal "--" separators; drop them.
    while cmd_tokens and cmd_tokens[0] == "--":
        cmd_tokens = cmd_tokens[1:]
    if not cmd_tokens:
        result["failure_category"] = "args_unknown"
        result["error_excerpt"] = "No command provided after '--'."
        _write_json(results_path, result)
        return 1
    if resolved_py:
        cmd_tokens = [t.replace("{python}", resolved_py.path) for t in cmd_tokens]

    result["command"] = _format_command_for_results(cmd_tokens)

    start = time.monotonic()
    exit_code: int = 1
    failure_category = ""
    timed_out = False

    with log_path.open("a", encoding="utf-8") as log_fp:
        log_fp.write(f"[runner] running: {result['command']}\n")
        log_fp.flush()
        try:
            proc = subprocess.run(
                cmd_tokens,
                cwd=repo,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                text=True,
                env=os.environ.copy(),
                timeout=timeout_sec,
            )
            exit_code = int(proc.returncode)
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 1
            failure_category = "timeout"
            log_fp.write(f"[runner] timeout after {timeout_sec}s\n")
        except FileNotFoundError as e:
            exit_code = 1
            failure_category = "entrypoint_not_found"
            log_fp.write(f"[runner] file not found: {e}\n")
        except Exception as e:
            exit_code = 1
            failure_category = "runtime"
            log_fp.write(f"[runner] exception: {type(e).__name__}: {e}\n")

    duration = time.monotonic() - start
    result["meta"]["duration_sec"] = round(duration, 3)

    if exit_code == 0 and not timed_out:
        result["status"] = "success"
        result["exit_code"] = 0
        result["failure_category"] = ""
        result["skip_reason"] = "not_applicable"
        result["error_excerpt"] = ""
        _write_json(results_path, result)
        return 0

    result["status"] = "failure"
    result["exit_code"] = 1
    if args.failure_category:
        result["failure_category"] = args.failure_category
    elif failure_category:
        result["failure_category"] = failure_category
    else:
        result["failure_category"] = "runtime"
    result["skip_reason"] = "not_applicable"
    error_excerpt = _tail_text(log_path, max_lines=220)
    result["error_excerpt"] = error_excerpt
    if stage in ("cpu", "single_gpu", "multi_gpu") and result["failure_category"] in ("runtime", "unknown"):
        inferred = _infer_failure_category_from_log(error_excerpt)
        if inferred:
            result["failure_category"] = inferred
    _write_json(results_path, result)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
