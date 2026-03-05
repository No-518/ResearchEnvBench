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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _tail_text(path: Path, max_lines: int = 240) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


def _redact_env_value(key: str, value: str) -> str:
    k = key.upper()
    if any(tok in k for tok in ["TOKEN", "SECRET", "PASSWORD", "PASS", "KEY"]):
        return "<redacted>"
    return value


def _collect_env_vars(extra_keys: Optional[List[str]] = None) -> Dict[str, str]:
    keys = [
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "SCIMLOPSBENCH_OFFLINE",
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "WORLD_SIZE",
        "RANK",
        "LOCAL_RANK",
        "OMP_NUM_THREADS",
    ]
    if extra_keys:
        keys.extend(extra_keys)
    out: Dict[str, str] = {}
    for k in keys:
        if k in os.environ:
            out[k] = _redact_env_value(k, os.environ.get(k, ""))
    for k in ["HF_TOKEN", "HUGGINGFACE_HUB_TOKEN"]:
        if k in os.environ:
            out[k] = "<set>" if os.environ.get(k) else "<empty>"
    return out


def _load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:
        return None, f"invalid_json: {e}"


def _resolve_python(
    *,
    cli_python: str,
    report_path: Path,
    require_report: bool,
) -> Tuple[Optional[str], List[str], Optional[str]]:
    warnings: List[str] = []

    if cli_python:
        return cli_python, warnings, None

    env_py = os.environ.get("SCIMLOPSBENCH_PYTHON", "").strip()
    if env_py:
        return env_py, warnings, None

    report, err = _load_json(report_path)
    if report is not None:
        py = str(report.get("python_path") or "").strip()
        if py:
            return py, warnings, None

    if require_report:
        reason = (
            f"Report missing/invalid at {report_path}"
            if err
            else f"python_path missing in report at {report_path}"
        )
        return None, warnings, reason

    fallback = shutil_which("python") or shutil_which("python3")
    if fallback:
        warnings.append(f"Falling back to PATH python: {fallback}")
        return fallback, warnings, None
    return None, warnings, "No python found (PATH/python3) and no report python_path"


def shutil_which(cmd: str) -> Optional[str]:
    from shutil import which

    return which(cmd)


@dataclass
class RunnerConfig:
    stage: str
    task: str
    framework: str
    timeout_sec: int
    out_dir: Path
    workdir: Path
    report_path: Path
    python_override: str
    requires_python: bool
    decision_reason: str
    skip: bool
    skip_reason: str
    failure_category: str
    assets_from: Optional[Path]
    env_overrides: Dict[str, str]
    cmd: List[str]


def _parse_args() -> RunnerConfig:
    p = argparse.ArgumentParser(description="Unified benchmark command runner.")
    p.add_argument("--stage", required=True)
    p.add_argument("--task", required=True, help="train|infer|check|download|validate|measure|unknown")
    p.add_argument("--framework", default="unknown", help="pytorch|tensorflow|jax|unknown")
    p.add_argument("--timeout-sec", type=int, default=None)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--workdir", default="")
    p.add_argument("--report-path", default="")
    p.add_argument("--python", default="", help="Override python interpreter used for placeholders.")
    p.add_argument(
        "--requires-python",
        choices=["auto", "required", "none"],
        default="auto",
        help="When 'required', fail if report missing and --python not provided.",
    )
    p.add_argument("--decision-reason", default="")
    p.add_argument("--skip", action="store_true")
    p.add_argument("--skip-reason", default="unknown")
    p.add_argument("--failure-category", default="unknown")
    p.add_argument("--assets-from", default="")
    p.add_argument("--env", action="append", default=[], help="KEY=VALUE overrides (repeatable)")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="Command after --")
    ns = p.parse_args()

    repo_root = _repo_root()
    out_dir = Path(ns.out_dir).resolve()
    workdir = Path(ns.workdir).resolve() if ns.workdir else repo_root

    report_path = Path(ns.report_path).resolve() if ns.report_path else Path(os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH))

    assets_from = Path(ns.assets_from).resolve() if ns.assets_from else None

    env_overrides: Dict[str, str] = {}
    for item in ns.env:
        if "=" not in item:
            raise SystemExit(f"--env must be KEY=VALUE, got: {item!r}")
        k, v = item.split("=", 1)
        env_overrides[k] = v

    cmd = ns.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    requires_python = False
    if ns.requires_python == "required":
        requires_python = True
    elif ns.requires_python == "none":
        requires_python = False
    else:
        requires_python = any(tok in {"__PYTHON__", "{python}"} for tok in cmd)

    def _default_timeout(stage: str) -> int:
        mapping = {
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
        return int(mapping.get(stage, 600))

    timeout_sec = int(ns.timeout_sec) if ns.timeout_sec is not None else _default_timeout(str(ns.stage))

    return RunnerConfig(
        stage=ns.stage,
        task=ns.task,
        framework=ns.framework,
        timeout_sec=timeout_sec,
        out_dir=out_dir,
        workdir=workdir,
        report_path=report_path,
        python_override=str(ns.python or "").strip(),
        requires_python=requires_python,
        decision_reason=ns.decision_reason,
        skip=bool(ns.skip),
        skip_reason=ns.skip_reason,
        failure_category=ns.failure_category,
        assets_from=assets_from,
        env_overrides=env_overrides,
        cmd=cmd,
    )


def _load_assets(assets_from: Optional[Path]) -> Dict[str, Any]:
    if not assets_from:
        return {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}}
    data, err = _load_json(assets_from)
    if data is None:
        return {"dataset": {"path": "", "source": "", "version": "", "sha256": ""}, "model": {"path": "", "source": "", "version": "", "sha256": ""}}
    assets = data.get("assets") or {}
    ds = assets.get("dataset") or {}
    md = assets.get("model") or {}
    def _norm(a: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "path": str(a.get("path") or ""),
            "source": str(a.get("source") or ""),
            "version": str(a.get("version") or ""),
            "sha256": str(a.get("sha256") or ""),
        }
    return {"dataset": _norm(ds), "model": _norm(md)}


def _write_results(
    *,
    results_path: Path,
    payload: Dict[str, Any],
) -> None:
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    cfg = _parse_args()
    repo_root = _repo_root()

    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.out_dir / "log.txt"
    results_path = cfg.out_dir / "results.json"

    # Always open/append so caller logs remain.
    with log_path.open("a", encoding="utf-8") as log_fp:
        log_fp.write(f"[runner] stage={cfg.stage} task={cfg.task} ts={_utc_ts()}\n")

    assets = _load_assets(cfg.assets_from)

    status = "failure"
    exit_code = 1
    skip_reason = cfg.skip_reason if cfg.skip else "unknown"
    failure_category = cfg.failure_category
    cmd_str = ""
    used_python = ""
    warnings: List[str] = []

    try:
        if cfg.skip:
            status = "skipped"
            exit_code = 0
            failure_category = "none"
            cmd_str = "(skipped)"
            with log_path.open("a", encoding="utf-8") as log_fp:
                log_fp.write(f"[runner] skipped: {skip_reason}\n")
            return 0

        if not cfg.cmd:
            raise RuntimeError("No command provided (missing after --)")

        require_report = cfg.requires_python and not cfg.python_override
        py, py_warnings, py_err = _resolve_python(
            cli_python=cfg.python_override,
            report_path=cfg.report_path,
            require_report=require_report,
        )
        warnings.extend(py_warnings)
        if cfg.requires_python:
            if not py:
                failure_category = "missing_report"
                with log_path.open("a", encoding="utf-8") as log_fp:
                    log_fp.write(f"[runner] python resolution failed: {py_err}\n")
                raise RuntimeError(py_err or "python resolution failed")
            used_python = py

        # Substitute placeholders.
        cmd_tokens: List[str] = []
        for tok in cfg.cmd:
            if tok in {"__PYTHON__", "{python}"}:
                if not py:
                    # If placeholder used but python not resolved, treat as missing report.
                    failure_category = "missing_report"
                    raise RuntimeError("Python placeholder present but python could not be resolved")
                cmd_tokens.append(py)
            else:
                cmd_tokens.append(tok)

        cmd_str = shlex.join(cmd_tokens)

        env = os.environ.copy()
        # Keep caches inside repo-owned benchmark_assets/cache by default.
        cache_root = repo_root / "benchmark_assets" / "cache"
        env.setdefault("XDG_CACHE_HOME", str(cache_root / "xdg"))
        env.setdefault("TORCH_HOME", str(cache_root / "torch"))
        env.setdefault("HF_HOME", str(cache_root / "hf"))
        env.setdefault("HUGGINGFACE_HUB_CACHE", str(cache_root / "hf"))
        env.setdefault("PYTHONPYCACHEPREFIX", str(cache_root / "pycache"))
        for k, v in cfg.env_overrides.items():
            env[k] = v

        # Ensure cache dirs exist.
        Path(env["XDG_CACHE_HOME"]).mkdir(parents=True, exist_ok=True)
        Path(env["TORCH_HOME"]).mkdir(parents=True, exist_ok=True)
        Path(env["HF_HOME"]).mkdir(parents=True, exist_ok=True)
        Path(env["PYTHONPYCACHEPREFIX"]).mkdir(parents=True, exist_ok=True)

        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(f"[runner] workdir={cfg.workdir}\n")
            log_fp.write(f"[runner] command={cmd_str}\n")
            if warnings:
                for w in warnings:
                    log_fp.write(f"[runner] warning: {w}\n")

        try:
            with log_path.open("a", encoding="utf-8") as log_fp:
                proc = subprocess.run(
                    cmd_tokens,
                    cwd=str(cfg.workdir),
                    env=env,
                    stdout=log_fp,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=cfg.timeout_sec,
                    check=False,
                )
            rc = int(proc.returncode)
        except FileNotFoundError as e:
            rc = 1
            failure_category = "entrypoint_not_found"
            with log_path.open("a", encoding="utf-8") as log_fp:
                log_fp.write(f"[runner] FileNotFoundError: {e}\n")
        except subprocess.TimeoutExpired:
            rc = 1
            failure_category = "timeout"
            with log_path.open("a", encoding="utf-8") as log_fp:
                log_fp.write(f"[runner] timeout after {cfg.timeout_sec}s\n")

        exit_code = 0 if rc == 0 else 1
        status = "success" if rc == 0 else "failure"
        if status == "failure" and failure_category == "unknown":
            failure_category = "runtime"

    except Exception as e:
        if failure_category == "unknown":
            failure_category = "unknown"
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(f"[runner] exception: {type(e).__name__}: {e}\n")
        status = "failure"
        exit_code = 1
    finally:
        payload: Dict[str, Any] = {
            "status": status,
            "skip_reason": skip_reason,
            "exit_code": int(exit_code),
            "stage": cfg.stage,
            "task": cfg.task,
            "command": cmd_str,
            "timeout_sec": int(cfg.timeout_sec),
            "framework": cfg.framework,
            "assets": assets,
            "meta": {
                "python": used_python,
                "git_commit": _git_commit(repo_root),
                "env_vars": _collect_env_vars(extra_keys=list(cfg.env_overrides.keys())),
                "decision_reason": cfg.decision_reason,
                "runner_ts_utc": _utc_ts(),
                "warnings": warnings,
            },
            "failure_category": failure_category,
            "error_excerpt": _tail_text(log_path) if status != "success" else "",
        }
        try:
            _write_results(results_path=results_path, payload=payload)
        except Exception:
            # Last resort: emit something to stderr if we can't write.
            sys.stderr.write("runner: failed to write results.json\n")

    return 0 if status in {"success", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
