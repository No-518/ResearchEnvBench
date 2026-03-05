#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
STAGE = "env_size"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_tail(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def git_commit() -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:
        return ""


def resolve_report_path(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def load_report(report_path: Path) -> Tuple[Dict[str, Any] | None, str | None]:
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data, None
        return None, "invalid_json"
    except FileNotFoundError:
        return None, "missing_report"
    except Exception:
        return None, "invalid_json"


def is_executable(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def dir_size_bytes(root: Path, warnings: List[str]) -> int:
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Skip typical caches (keep env size focused on sys.prefix + site-packages).
            dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".mypy_cache", ".pytest_cache"}]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                try:
                    total += fpath.stat().st_size
                except PermissionError:
                    warnings.append(f"permission_error:{fpath}")
                except FileNotFoundError:
                    continue
                except OSError as e:
                    warnings.append(f"os_error:{fpath}:{type(e).__name__}")
    except PermissionError:
        warnings.append(f"permission_error_walk:{root}")
    except FileNotFoundError:
        return 0
    return total


def probe_paths(python_path: str, timeout_sec: int) -> Tuple[int, str]:
    code = r"""
import json
import site
import sys

out = {"sys_executable": sys.executable, "sys_prefix": sys.prefix, "site_packages": [], "user_site": ""}
try:
  out["site_packages"] = list(site.getsitepackages())
except Exception:
  out["site_packages"] = []
try:
  out["user_site"] = site.getusersitepackages()
except Exception:
  out["user_site"] = ""
print(json.dumps(out))
"""
    cp = subprocess.run(
        [python_path, "-c", code],
        cwd=REPO_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec,
        env=dict(os.environ),
    )
    return int(cp.returncode), cp.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", help="Override report.json path (else SCIMLOPSBENCH_REPORT or /opt/scimlopsbench/report.json)")
    ap.add_argument("--timeout-sec", type=int, default=int(os.environ.get("SCIMLOPSBENCH_ENV_SIZE_TIMEOUT_SEC", "120")))
    args = ap.parse_args()

    out_dir = REPO_ROOT / "build_output" / STAGE
    safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

    report_path = resolve_report_path(args.report_path)
    meta: Dict[str, Any] = {
        "python": sys.executable,
        "git_commit": git_commit(),
        "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
        "timestamp_utc": utc_now(),
        "report_path": str(report_path),
    }

    report, report_err = load_report(report_path)
    reported_python_path = ""
    if report is None:
        log_path.write_text(f"[env_size] report load failed: {report_err}\n", encoding="utf-8")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": STAGE,
            "task": "measure",
            "command": "",
            "timeout_sec": int(args.timeout_sec),
            "reported_python_path": reported_python_path,
            "framework": "unknown",
            "assets": assets,
            "observed": {},
            "meta": {**meta, "decision_reason": "Measure env size requires a valid report.json with python_path."},
            "failure_category": "env_size_failed",
            "error_excerpt": read_tail(log_path),
        }
        results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 1

    python_path_val = report.get("python_path")
    if isinstance(python_path_val, str):
        reported_python_path = python_path_val
    else:
        reported_python_path = ""

    py = Path(reported_python_path) if reported_python_path else None
    if py is None or not is_executable(py):
        log_path.write_text(f"[env_size] invalid python_path in report: {reported_python_path!r}\n", encoding="utf-8")
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": STAGE,
            "task": "measure",
            "command": "",
            "timeout_sec": int(args.timeout_sec),
            "reported_python_path": reported_python_path,
            "framework": "unknown",
            "assets": assets,
            "observed": {},
            "meta": {**meta, "decision_reason": "report.json python_path must exist and be executable."},
            "failure_category": "env_size_failed",
            "error_excerpt": read_tail(log_path),
        }
        results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 1

    cmd_str = f"{py} measure_env_size.py --report-path {report_path}"
    rc, out = probe_paths(str(py), timeout_sec=int(args.timeout_sec))
    log_path.write_text(
        f"[env_size] timestamp_utc={utc_now()}\n[env_size] python={py}\n[env_size] probe_rc={rc}\n{out}\n",
        encoding="utf-8",
    )
    if rc != 0:
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": STAGE,
            "task": "measure",
            "command": cmd_str,
            "timeout_sec": int(args.timeout_sec),
            "reported_python_path": reported_python_path,
            "framework": "unknown",
            "assets": assets,
            "observed": {},
            "meta": {**meta, "decision_reason": "Failed to probe sys.prefix/site-packages from report python."},
            "failure_category": "env_size_failed",
            "error_excerpt": read_tail(log_path),
        }
        results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 1

    try:
        probed = json.loads(out.strip().splitlines()[-1])
        assert isinstance(probed, dict)
    except Exception as e:
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": STAGE,
            "task": "measure",
            "command": cmd_str,
            "timeout_sec": int(args.timeout_sec),
            "reported_python_path": reported_python_path,
            "framework": "unknown",
            "assets": assets,
            "observed": {},
            "meta": {**meta, "decision_reason": f"Probe output was not valid JSON: {type(e).__name__}:{e}"},
            "failure_category": "env_size_failed",
            "error_excerpt": read_tail(log_path),
        }
        results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 1

    env_prefix = Path(str(probed.get("sys_prefix", "")))
    site_paths: List[Path] = []
    for p in probed.get("site_packages", []) or []:
        if isinstance(p, str) and p:
            site_paths.append(Path(p))
    user_site = probed.get("user_site")
    if isinstance(user_site, str) and user_site:
        site_paths.append(Path(user_site))

    warnings: List[str] = []
    env_prefix_size = dir_size_bytes(env_prefix, warnings) if env_prefix else 0
    site_sizes: List[Dict[str, Any]] = []
    site_total = 0
    for p in site_paths:
        size = dir_size_bytes(p, warnings) if p else 0
        site_sizes.append({"path": str(p), "size_bytes": int(size)})
        site_total += int(size)

    observed = {
        "env_prefix": str(env_prefix),
        "env_prefix_size_MB": int(round(env_prefix_size / (1024 * 1024))),
        "site_packages": site_sizes,
        "site_packages_total_bytes": int(site_total),
    }

    payload = {
        "status": "success",
        "skip_reason": "not_applicable",
        "exit_code": 0,
        "stage": STAGE,
        "task": "measure",
        "command": cmd_str,
        "timeout_sec": int(args.timeout_sec),
        "reported_python_path": reported_python_path,
        "framework": "unknown",
        "assets": assets,
        "observed": observed,
        "meta": {**meta, "decision_reason": "Size is computed recursively for sys.prefix and each site-packages directory.", "warnings": warnings},
        "failure_category": "not_applicable",
        "error_excerpt": read_tail(log_path),
    }
    results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
