#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True


REPO_ROOT = Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    _write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n")


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = _read_text(path).splitlines()
    except Exception:
        return ""
    if len(lines) <= max_lines:
        return "\n".join(lines).strip()
    return "\n".join(lines[-max_lines:]).strip()


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
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        sha = (cp.stdout or "").strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _parse_json_file(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = _read_text(path)
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:
        return None, f"read_error: {type(e).__name__}: {e}"
    try:
        obj = json.loads(raw)
    except Exception as e:
        return None, f"invalid_json: {type(e).__name__}: {e}"
    if not isinstance(obj, dict):
        return None, "invalid_json: top-level is not an object"
    return obj, None


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_report = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_report:
        return Path(env_report)
    return Path("/opt/scimlopsbench/report.json")


def resolve_python_interpreter(
    *,
    cli_python: Optional[str],
    env_python: Optional[str],
    report_path: Path,
    require_report_if_needed: bool,
) -> Tuple[Optional[str], List[str], Optional[dict], Optional[str]]:
    """
    Returns: (resolved_python, warnings, report_dict, failure_category)
    """
    warnings: List[str] = []

    if cli_python:
        return cli_python, warnings, None, None

    if env_python:
        return env_python, warnings, None, None

    report, report_err = _parse_json_file(report_path)
    if report_err:
        if require_report_if_needed:
            return None, warnings, None, "missing_report"
        return shutil.which("python") or shutil.which("python3"), ["report_missing_or_invalid; using PATH python"], None, None

    python_path = report.get("python_path")
    if isinstance(python_path, str) and python_path.strip():
        candidate = python_path.strip()
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate, warnings, report, None
        warnings.append(f"reported python_path is not executable; falling back to PATH python: {candidate}")

    fallback = shutil.which("python") or shutil.which("python3")
    if fallback:
        warnings.append("using PATH python fallback")
        return fallback, warnings, report, None

    return None, warnings, report, "missing_report" if require_report_if_needed else "unknown"


def _env_snapshot() -> Dict[str, str]:
    keys = [
        "CUDA_VISIBLE_DEVICES",
        "SCIMLOPSBENCH_PYTHON",
        "SCIMLOPSBENCH_REPORT",
        "HF_HOME",
        "HF_AUTH_TOKEN",
        "HUGGINGFACE_HUB_TOKEN",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "WANDB_MODE",
    ]
    snap: Dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            snap[k] = v
    return snap


def _default_timeout_for_stage(stage: str) -> int:
    return {
        "prepare": 1200,
        "cpu": 600,
        "single_gpu": 600,
        "multi_gpu": 1200,
        "env_size": 120,
        "hallucination": 120,
        "pyright": 600,
        "cuda": 120,
        "summary": 120,
    }.get(stage, 600)


def _cmd_to_str(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def run_command_with_timeout(
    *,
    cmd: List[str],
    cwd: Path,
    env: Dict[str, str],
    timeout_sec: int,
    log_path: Path,
) -> Tuple[int, bool]:
    _safe_mkdir(log_path.parent)
    with log_path.open("w", encoding="utf-8") as f:
        f.write(f"[runner] repo_root={cwd}\n")
        f.write(f"[runner] start_utc={_utc_now_iso()}\n")
        f.write(f"[runner] timeout_sec={timeout_sec}\n")
        f.write(f"[runner] cmd={_cmd_to_str(cmd)}\n")
        f.write("\n")
        f.flush()

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            f.write(f"\n[runner] FileNotFoundError: command not found: {cmd[0]}\n")
            return 127, False
        except Exception as e:
            f.write(f"\n[runner] Failed to start process: {type(e).__name__}: {e}\n")
            return 1, False

        timed_out = False
        try:
            rc = proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            f.write(f"\n[runner] TIMEOUT after {timeout_sec}s; terminating...\n")
            proc.kill()
            rc = 124
        except Exception as e:
            f.write(f"\n[runner] Error while waiting: {type(e).__name__}: {e}\n")
            try:
                proc.kill()
            except Exception:
                pass
            rc = 1

        f.write(f"\n[runner] end_utc={_utc_now_iso()}\n")
        f.write(f"[runner] return_code={rc}\n")
        return int(rc), timed_out


def _base_assets() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _merge_results(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(base)
    for k, v in extra.items():
        if k == "assets" and isinstance(v, dict) and isinstance(merged.get("assets"), dict):
            assets = dict(merged["assets"])
            for ak, av in v.items():
                if isinstance(av, dict) and isinstance(assets.get(ak), dict):
                    assets[ak] = {**assets[ak], **av}
                else:
                    assets[ak] = av
            merged["assets"] = assets
            continue
        if k == "meta" and isinstance(v, dict) and isinstance(merged.get("meta"), dict):
            merged["meta"] = {**merged["meta"], **v}
            continue
        merged[k] = v
    return merged


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark runner")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--task", required=True, help="train|infer|check|download|validate|measure|summarize")
    parser.add_argument("--framework", default="unknown", help="pytorch|tensorflow|jax|unknown")
    parser.add_argument("--out-dir", default=None, help="Default: build_output/<stage>")
    parser.add_argument("--timeout-sec", type=int, default=None)

    parser.add_argument("--python", dest="cli_python", default=None, help="Explicit python executable (highest priority)")
    parser.add_argument("--report-path", default=None, help="Default: /opt/scimlopsbench/report.json")
    parser.add_argument(
        "--no-python",
        action="store_true",
        help="Do not require report/python resolution (for shell-only stages).",
    )

    parser.add_argument("--decision-reason", default="", help="Recorded in results.json meta.decision_reason")
    parser.add_argument("--assets-json", default=None, help="Path to JSON with {'dataset':..., 'model':...}")
    parser.add_argument(
        "--extra-json",
        default=None,
        help="Path to JSON merged into results.json after command finishes.",
    )

    parser.add_argument("--skip", action="store_true")
    parser.add_argument("--skip-reason", default="unknown", help="repo_not_supported|insufficient_hardware|not_applicable|unknown")
    parser.add_argument(
        "--failure-category",
        default="unknown",
        help="entrypoint_not_found|args_unknown|auth_required|download_failed|deps|data|model|runtime|oom|timeout|cpu_not_supported|missing_report|invalid_json|unknown",
    )

    parser.add_argument("--", dest="double_dash", action="store_true")
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    stage = args.stage
    out_dir = Path(args.out_dir) if args.out_dir else (REPO_ROOT / "build_output" / stage)
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    timeout_sec = int(args.timeout_sec) if args.timeout_sec is not None else _default_timeout_for_stage(stage)

    report_path = _resolve_report_path(args.report_path)

    resolved_python: Optional[str] = None
    python_warnings: List[str] = []
    report_dict: Optional[dict] = None
    python_resolution_failure: Optional[str] = None
    if not args.no_python:
        resolved_python, python_warnings, report_dict, python_resolution_failure = resolve_python_interpreter(
            cli_python=args.cli_python,
            env_python=os.environ.get("SCIMLOPSBENCH_PYTHON"),
            report_path=report_path,
            require_report_if_needed=True,
        )

    assets = _base_assets()
    if args.assets_json:
        extra_assets, err = _parse_json_file(Path(args.assets_json))
        if extra_assets and isinstance(extra_assets, dict):
            assets = {**assets, **{k: v for k, v in extra_assets.items() if isinstance(v, dict)}}

    meta: Dict[str, Any] = {
        "python": resolved_python or "",
        "runner_python": sys.executable,
        "git_commit": _git_commit(REPO_ROOT),
        "env_vars": _env_snapshot(),
        "decision_reason": args.decision_reason,
        "timestamp_utc": _utc_now_iso(),
        "report_path": str(report_path),
        "python_resolution_warnings": python_warnings,
    }

    if python_resolution_failure:
        _safe_mkdir(out_dir)
        _write_text(log_path, f"[runner] missing/invalid report: {report_path}\n")
        results = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": stage,
            "task": args.task,
            "command": "",
            "timeout_sec": timeout_sec,
            "framework": args.framework,
            "assets": assets,
            "meta": meta,
            "command_exit_code": None,
            "failure_category": "missing_report",
            "error_excerpt": _tail_lines(log_path),
        }
        _write_json(results_path, results)
        return 1

    if args.skip:
        _write_text(log_path, f"[runner] skipped stage={stage} reason={args.skip_reason}\n")
        results = {
            "status": "skipped",
            "skip_reason": args.skip_reason,
            "exit_code": 0,
            "stage": stage,
            "task": args.task,
            "command": "",
            "timeout_sec": timeout_sec,
            "framework": args.framework,
            "assets": assets,
            "meta": meta,
            "command_exit_code": 0,
            "failure_category": "not_applicable",
            "error_excerpt": "",
        }
        _write_json(results_path, results)
        return 0

    if not args.cmd:
        _write_text(log_path, "[runner] ERROR: no command provided\n")
        results = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": stage,
            "task": args.task,
            "command": "",
            "timeout_sec": timeout_sec,
            "framework": args.framework,
            "assets": assets,
            "meta": meta,
            "command_exit_code": None,
            "failure_category": "args_unknown",
            "error_excerpt": _tail_lines(log_path),
        }
        _write_json(results_path, results)
        return 1

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    cmd_str = _cmd_to_str(cmd)

    env = dict(os.environ)
    if resolved_python:
        env["SCIMLOPSBENCH_PYTHON_RESOLVED"] = resolved_python
    env["SCIMLOPSBENCH_REPO_ROOT"] = str(REPO_ROOT)

    rc, timed_out = run_command_with_timeout(
        cmd=cmd,
        cwd=REPO_ROOT,
        env=env,
        timeout_sec=timeout_sec,
        log_path=log_path,
    )

    status = "success" if rc == 0 and not timed_out else "failure"
    stage_exit_code = 0 if status in ("success", "skipped") else 1

    failure_category = "unknown"
    if status == "failure":
        if timed_out or rc == 124:
            failure_category = "timeout"
        elif rc == 127:
            failure_category = "entrypoint_not_found"
        else:
            failure_category = args.failure_category or "unknown"

    error_excerpt = _tail_lines(log_path) if status == "failure" else ""
    if (
        status == "failure"
        and stage in ("cpu", "single_gpu", "multi_gpu")
        and failure_category in ("unknown", "runtime")
    ):
        inferred = _infer_failure_category_from_log(error_excerpt)
        if inferred:
            failure_category = inferred

    results = {
        "status": status,
        "skip_reason": "not_applicable" if status != "skipped" else args.skip_reason,
        "exit_code": stage_exit_code,
        "stage": stage,
        "task": args.task,
        "command": cmd_str,
        "timeout_sec": timeout_sec,
        "framework": args.framework,
        "assets": assets,
        "meta": meta,
        "command_exit_code": rc,
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }
    if args.extra_json:
        extra_obj, extra_err = _parse_json_file(Path(args.extra_json))
        if extra_obj and isinstance(extra_obj, dict):
            results = _merge_results(results, extra_obj)
        else:
            results = _merge_results(
                results,
                {"meta": {"extra_json_error": extra_err or "invalid extra json"}},
            )
    _write_json(results_path, results)
    return stage_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
