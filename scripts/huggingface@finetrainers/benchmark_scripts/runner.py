#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def safe_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def safe_write_json(path: Path, payload: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, f"Expected JSON object in {path}"
        return data, None
    except FileNotFoundError:
        return None, f"Missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON in {path}: {e}"
    except Exception as e:
        return None, f"Failed to read {path}: {e}"


def tail_text_file(path: Path, *, max_lines: int = 220) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:]).strip()
    except Exception:
        return ""

def read_file_tail_bytes(path: Path, *, max_bytes: int = 2_000_000) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(size - max_bytes, 0), os.SEEK_SET)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def infer_failure_category_from_log(log_text: str) -> str:
    t = (log_text or "").lower()
    if not t:
        return ""

    # Common dependency/runtime linkage issues (often surfaced when decoding video datasets).
    if "could not load libtorchcodec" in t or "libavutil.so" in t:
        return "deps"
    if "modulenotfounderror" in t or "no module named" in t:
        return "deps"

    # OOM patterns.
    if "out of memory" in t and ("cuda" in t or "cublas" in t or "hip" in t or "rocm" in t):
        return "oom"

    # Auth/gated model/dataset patterns.
    if any(k in t for k in ["unauthorized", "forbidden", "gated", "http 401", "http 403"]):
        return "auth_required"

    return ""


def get_git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def get_relevant_env_snapshot(extra_keys: Sequence[str] = ()) -> Dict[str, Any]:
    keys = [
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "CUDA_VISIBLE_DEVICES",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "HF_HOME",
        "HF_DATASETS_CACHE",
        "TRANSFORMERS_CACHE",
        "WANDB_MODE",
        "NCCL_P2P_DISABLE",
        "TORCH_NCCL_ENABLE_MONITORING",
        "FINETRAINERS_LOG_LEVEL",
        "HF_HUB_OFFLINE",
        "HF_HUB_ENABLE_HF_TRANSFER",
    ]
    keys.extend(list(extra_keys))
    snapshot: Dict[str, Any] = {}
    for k in keys:
        if k in os.environ:
            if k in {"HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "WANDB_API_KEY"}:
                snapshot[k] = "***redacted***"
            else:
                snapshot[k] = os.environ.get(k)
    snapshot["has_hf_token"] = bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN"))
    snapshot["has_wandb_api_key"] = bool(os.environ.get("WANDB_API_KEY"))
    return snapshot


@dataclass
class PythonResolution:
    python_path: str
    source: str
    warnings: List[str]
    report_path: str
    report_loaded: bool


class MissingReportError(RuntimeError):
    pass


def _is_executable_file(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.X_OK)


def _resolve_executable(candidate: str) -> Optional[str]:
    if not candidate:
        return None
    p = Path(candidate)
    if p.is_absolute() or "/" in candidate:
        return str(p) if _is_executable_file(p) else None
    return shutil.which(candidate)


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return DEFAULT_REPORT_PATH


def resolve_python_interpreter(
    *,
    cli_python: Optional[str],
    requires_python: bool,
    cli_report_path: Optional[str],
) -> PythonResolution:
    warnings: List[str] = []

    if cli_python:
        resolved = _resolve_executable(cli_python)
        if not resolved:
            raise RuntimeError(f"--python is not an executable: {cli_python}")
        return PythonResolution(
            python_path=resolved,
            source="cli",
            warnings=warnings,
            report_path=str(resolve_report_path(cli_report_path)),
            report_loaded=False,
        )

    if os.environ.get("SCIMLOPSBENCH_PYTHON"):
        candidate = os.environ["SCIMLOPSBENCH_PYTHON"]
        resolved = _resolve_executable(candidate)
        if not resolved:
            raise RuntimeError(f"SCIMLOPSBENCH_PYTHON is not an executable: {candidate}")
        return PythonResolution(
            python_path=resolved,
            source="env",
            warnings=warnings,
            report_path=str(resolve_report_path(cli_report_path)),
            report_loaded=False,
        )

    report_path = resolve_report_path(cli_report_path)
    report, err = safe_read_json(report_path)
    if report is None:
        if requires_python:
            raise MissingReportError(err or f"Missing/invalid report: {report_path}")
        resolved_fallback = _resolve_executable("python3") or _resolve_executable("python")
        if not resolved_fallback:
            raise RuntimeError("No python interpreter found on PATH")
        warnings.append(f"Report missing/invalid; using fallback python from PATH: {resolved_fallback}")
        return PythonResolution(
            python_path=resolved_fallback,
            source="fallback_path",
            warnings=warnings,
            report_path=str(report_path),
            report_loaded=False,
        )

    python_from_report = report.get("python_path")
    if not python_from_report:
        if requires_python:
            raise MissingReportError(f'Report missing required key "python_path": {report_path}')
        resolved_fallback = _resolve_executable("python3") or _resolve_executable("python")
        if not resolved_fallback:
            raise RuntimeError("No python interpreter found on PATH")
        warnings.append(f'Report missing "python_path"; using fallback python from PATH: {resolved_fallback}')
        return PythonResolution(
            python_path=resolved_fallback,
            source="fallback_path",
            warnings=warnings,
            report_path=str(report_path),
            report_loaded=True,
        )

    resolved_report = _resolve_executable(str(python_from_report)) or str(python_from_report)
    if _is_executable_file(Path(resolved_report)):
        return PythonResolution(
            python_path=resolved_report,
            source="report",
            warnings=warnings,
            report_path=str(report_path),
            report_loaded=True,
        )

    resolved_fallback = _resolve_executable("python3") or _resolve_executable("python")
    if not resolved_fallback:
        raise RuntimeError(f'Report python_path is not executable and no fallback python on PATH: {python_from_report}')

    warnings.append(f"python_path from report is not executable: {python_from_report}")
    warnings.append(f"Using fallback python from PATH: {resolved_fallback}")
    return PythonResolution(
        python_path=resolved_fallback,
        source="fallback_path",
        warnings=warnings,
        report_path=str(report_path),
        report_loaded=True,
    )


def load_assets_from_manifest(manifest_path: Path) -> Dict[str, Any]:
    empty = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    manifest, err = safe_read_json(manifest_path)
    if manifest is None:
        return empty

    dataset = manifest.get("dataset") or {}
    model = manifest.get("model") or {}
    return {
        "dataset": {
            "path": str((manifest.get("paths") or {}).get("dataset_symlink") or dataset.get("resolved_path") or ""),
            "source": str(dataset.get("source") or ""),
            "version": str(dataset.get("version") or dataset.get("revision") or ""),
            "sha256": str(dataset.get("sha256") or ""),
        },
        "model": {
            "path": str((manifest.get("paths") or {}).get("model_symlink") or model.get("resolved_path") or ""),
            "source": str(model.get("source") or ""),
            "version": str(model.get("version") or model.get("revision") or ""),
            "sha256": str(model.get("sha256") or ""),
        },
    }


def build_base_results(
    *,
    stage: str,
    task: str,
    command_str: str,
    timeout_sec: int,
    framework: str,
    assets: Dict[str, Any],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": stage,
        "task": task,
        "command": command_str,
        "timeout_sec": timeout_sec,
        "framework": framework,
        "assets": assets,
        "meta": meta,
        "failure_category": "unknown",
        "error_excerpt": "",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified benchmark command runner")
    parser.add_argument("--stage", type=str, required=False)
    parser.add_argument("--task", type=str, required=False)
    parser.add_argument("--out-root", type=str, default="build_output")
    parser.add_argument("--timeout-sec", type=int, default=600)
    parser.add_argument("--framework", type=str, default="unknown")
    parser.add_argument("--assets-manifest", type=str, default="benchmark_assets/manifest.json")
    parser.add_argument("--decision-reason", type=str, default="")
    parser.add_argument("--failure-category", type=str, default="runtime")
    parser.add_argument("--report-path", type=str, default=None)
    parser.add_argument("--python", type=str, default=None)
    parser.add_argument(
        "--requires-python",
        action="store_true",
        default=True,
        help="Require resolving python via CLI/env/report (default: true).",
    )
    parser.add_argument(
        "--no-requires-python",
        action="store_true",
        default=False,
        help="Disable python resolution requirement for stages that do not need Python.",
    )
    parser.add_argument("--env", action="append", default=[], help="KEY=VALUE to add/override for the command")
    parser.add_argument(
        "--fail-regex",
        action="append",
        default=[],
        help="Regex pattern; if command exits 0 but log matches, mark stage as failure (repeatable).",
    )
    parser.add_argument("--skip", action="store_true", help="Skip running the command; still write results.json")
    parser.add_argument(
        "--skip-reason",
        type=str,
        default="repo_not_supported",
        choices=["repo_not_supported", "insufficient_hardware", "not_applicable", "unknown"],
    )
    parser.add_argument(
        "--print-python",
        action="store_true",
        help="Print resolved python path and exit (no results.json written).",
    )

    args, remainder = parser.parse_known_args(argv)

    requires_python = args.requires_python and not args.no_requires_python

    if args.print_python:
        try:
            res = resolve_python_interpreter(
                cli_python=args.python, requires_python=requires_python, cli_report_path=args.report_path
            )
            print(res.python_path)
            return 0
        except Exception as e:
            print(str(e), file=sys.stderr)
            return 1

    if not args.stage or not args.task:
        print("--stage and --task are required unless --print-python is used", file=sys.stderr)
        return 2

    out_root = (REPO_ROOT / args.out_root).resolve()
    stage_dir = out_root / args.stage
    ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    assets = load_assets_from_manifest(REPO_ROOT / args.assets_manifest)

    # Resolve python for placeholder replacement and metadata.
    python_resolution: Optional[PythonResolution] = None
    python_resolution_exc: Optional[BaseException] = None
    if requires_python:
        try:
            python_resolution = resolve_python_interpreter(
                cli_python=args.python, requires_python=True, cli_report_path=args.report_path
            )
        except MissingReportError as e:
            python_resolution_exc = e
        except Exception as e:
            python_resolution_exc = e

    # Prepare command
    cmd_tokens = remainder
    if cmd_tokens and cmd_tokens[0] == "--":
        cmd_tokens = cmd_tokens[1:]
    cmd_tokens = list(cmd_tokens)
    if python_resolution and cmd_tokens:
        cmd_tokens = [python_resolution.python_path if t == "{python}" else t for t in cmd_tokens]

    command_str = " ".join(shlex.quote(t) for t in cmd_tokens) if cmd_tokens else ""

    meta: Dict[str, Any] = {
        "python": python_resolution.python_path if python_resolution else "",
        "python_resolution_source": python_resolution.source if python_resolution else "",
        "python_resolution_warnings": python_resolution.warnings if python_resolution else [],
        "report_path": python_resolution.report_path if python_resolution else str(resolve_report_path(args.report_path)),
        "git_commit": get_git_commit(REPO_ROOT),
        "env_vars": get_relevant_env_snapshot(),
        "decision_reason": args.decision_reason,
        "timestamp_utc": utc_now_iso(),
    }

    if python_resolution_exc:
        meta["python_resolution_error"] = str(python_resolution_exc)

    results = build_base_results(
        stage=args.stage,
        task=args.task,
        command_str=command_str,
        timeout_sec=int(args.timeout_sec),
        framework=args.framework,
        assets=assets,
        meta=meta,
    )

    # Skipped stage
    if args.skip:
        results.update(
            {
                "status": "skipped",
                "skip_reason": args.skip_reason,
                "exit_code": 0,
                "failure_category": "not_applicable",
                "error_excerpt": "",
            }
        )
        safe_write_text(log_path, f"[{utc_now_iso()}] skipped: {args.skip_reason}\n")
        safe_write_json(results_path, results)
        return 0

    # Require python but couldn't resolve due to missing report.
    if requires_python and python_resolution_exc:
        safe_write_text(
            log_path,
            f"[{utc_now_iso()}] failed to resolve python interpreter\n{python_resolution_exc}\n",
        )
        results.update(
            {
                "status": "failure",
                "exit_code": 1,
                "failure_category": "missing_report" if isinstance(python_resolution_exc, MissingReportError) else "deps",
            }
        )
        results["error_excerpt"] = tail_text_file(log_path)
        safe_write_json(results_path, results)
        return 1

    # Execute command
    env = os.environ.copy()
    # Apply env overrides
    for kv in args.env or []:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        env[k] = v

    start = time.time()
    command_exit_code: Optional[int] = None
    failure_category = ""
    try:
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write(f"[{utc_now_iso()}] cwd={REPO_ROOT}\n")
            logf.write(f"[{utc_now_iso()}] command={command_str}\n")
            logf.flush()

            proc = subprocess.Popen(
                cmd_tokens,
                cwd=REPO_ROOT,
                stdout=logf,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
            )
            try:
                proc.wait(timeout=float(args.timeout_sec))
            except subprocess.TimeoutExpired:
                failure_category = "timeout"
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                command_exit_code = 124
            else:
                command_exit_code = proc.returncode
    except FileNotFoundError:
        failure_category = "entrypoint_not_found"
        command_exit_code = 127
    except Exception as e:
        failure_category = "unknown"
        with log_path.open("a", encoding="utf-8") as logf:
            logf.write(f"\n[{utc_now_iso()}] runner exception: {e}\n")
        command_exit_code = 1

    elapsed = time.time() - start
    results["meta"]["elapsed_sec"] = round(elapsed, 3)
    results["meta"]["command_exit_code"] = command_exit_code

    fail_regexes: List[str] = [str(p) for p in (args.fail_regex or []) if str(p)]
    log_scan_tail = ""
    log_fail_matches: List[str] = []
    if fail_regexes:
        log_scan_tail = read_file_tail_bytes(log_path)
        for pat in fail_regexes:
            try:
                if re.search(pat, log_scan_tail, flags=re.MULTILINE):
                    log_fail_matches.append(pat)
            except re.error:
                # Treat invalid regex as non-match but record it.
                results.setdefault("meta", {}).setdefault("log_fail_regex_errors", []).append(pat)

    results["meta"]["log_fail_regexes"] = fail_regexes
    results["meta"]["log_fail_matches"] = log_fail_matches

    if command_exit_code == 0:
        if log_fail_matches:
            results.update(
                {
                    "status": "failure",
                    "skip_reason": "not_applicable",
                    "exit_code": 1,
                    "failure_category": args.failure_category or "runtime",
                }
            )
            results["meta"]["failure_detected_in_log"] = True
        else:
            results.update(
                {
                    "status": "success",
                    "skip_reason": "not_applicable",
                    "exit_code": 0,
                    "failure_category": "not_applicable",
                }
            )
    else:
        results.update(
            {
                "status": "failure",
                "exit_code": 1,
                "failure_category": failure_category or args.failure_category or "runtime",
            }
        )

    if results.get("status") == "failure" and results.get("failure_category") in {"runtime", "unknown"}:
        log_for_inference = log_scan_tail or read_file_tail_bytes(log_path)
        inferred = infer_failure_category_from_log(log_for_inference)
        if inferred:
            results["meta"]["failure_category_inferred_from_log"] = inferred
            results["failure_category"] = inferred

    results["error_excerpt"] = tail_text_file(log_path)
    safe_write_json(results_path, results)
    return 0 if results["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
