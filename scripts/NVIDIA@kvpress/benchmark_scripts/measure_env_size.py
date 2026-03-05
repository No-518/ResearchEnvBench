#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bench_utils import REPO_ROOT, ensure_dir, get_git_commit, tail_lines, utc_timestamp, write_json


def resolve_report_path(cli_report_path: str) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path("/opt/scimlopsbench/report.json")


def load_report(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None, None
    except FileNotFoundError:
        return None, "missing_report"
    except Exception:
        return None, "invalid_json"


def get_python_env_info(python_exe: str) -> Tuple[Optional[Dict[str, Any]], str]:
    code = r"""
import json, site, sys
out = {
  "python_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_packages": [],
}
try:
  out["site_packages"] = list(site.getsitepackages())
except Exception:
  out["site_packages"] = []
try:
  out["user_site"] = site.getusersitepackages()
except Exception:
  out["user_site"] = None
print(json.dumps(out))
"""
    try:
        proc = subprocess.run([python_exe, "-c", code], capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
        if proc.returncode != 0:
            return None, (proc.stderr.strip() or proc.stdout.strip() or f"python returned {proc.returncode}")
        try:
            return json.loads(proc.stdout.strip() or "{}"), ""
        except Exception as e:
            return None, f"failed to parse python env info json: {e}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def dir_size_bytes(path: Path) -> Tuple[int, List[str]]:
    total = 0
    warnings: List[str] = []
    if not path.exists():
        return 0, [f"missing_path: {path}"]
    for root, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
        for name in filenames:
            fp = Path(root) / name
            try:
                st = fp.stat()
                total += int(st.st_size)
            except PermissionError as e:
                warnings.append(f"permission_error: {fp}: {e}")
            except FileNotFoundError:
                continue
            except OSError as e:
                warnings.append(f"os_error: {fp}: {e}")
    return total, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure environment size from agent report python_path.")
    parser.add_argument("--report-path", default="", help="Override report path.")
    args = parser.parse_args()

    stage = "env_size"
    out_dir = REPO_ROOT / "build_output" / stage
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report, report_err = load_report(report_path)

    meta: Dict[str, Any] = {"timestamp_utc": utc_timestamp(), "git_commit": get_git_commit(REPO_ROOT), "env_vars": {}}

    if not report or not isinstance(report, dict):
        log_path.write_text(f"ERROR: report load failed ({report_err}) at {report_path}\n", encoding="utf-8")
        write_json(
            results_path,
            {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "env_size",
                "task": "measure",
                "command": f"python {Path(__file__).name} --report-path {report_path}",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "reported_python_path": "",
                "observed": {},
                "meta": meta,
                "failure_category": "env_size_failed",
                "error_excerpt": tail_lines(log_path),
            },
        )
        return 1

    python_exe = report.get("python_path")
    if not isinstance(python_exe, str) or not python_exe:
        log_path.write_text(f"ERROR: python_path missing in report {report_path}\n", encoding="utf-8")
        write_json(
            results_path,
            {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "env_size",
                "task": "measure",
                "command": f"python {Path(__file__).name} --report-path {report_path}",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "reported_python_path": "",
                "observed": {},
                "meta": meta,
                "failure_category": "env_size_failed",
                "error_excerpt": tail_lines(log_path),
            },
        )
        return 1

    env_info, env_err = get_python_env_info(python_exe)
    if not env_info:
        log_path.write_text(f"ERROR: failed to query env info via {python_exe}: {env_err}\n", encoding="utf-8")
        write_json(
            results_path,
            {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "env_size",
                "task": "measure",
                "command": f"python {Path(__file__).name} --report-path {report_path}",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "reported_python_path": python_exe,
                "observed": {},
                "meta": meta,
                "failure_category": "env_size_failed",
                "error_excerpt": tail_lines(log_path),
            },
        )
        return 1

    env_prefix = Path(str(env_info.get("sys_prefix", "")))
    site_paths: List[Path] = []
    for p in env_info.get("site_packages", []) or []:
        if isinstance(p, str) and p:
            site_paths.append(Path(p))
    user_site = env_info.get("user_site")
    if isinstance(user_site, str) and user_site:
        site_paths.append(Path(user_site))

    log_lines = []
    log_lines.append(f"timestamp_utc={utc_timestamp()}")
    log_lines.append(f"reported_python_path={python_exe}")
    log_lines.append(f"observed_sys_prefix={env_prefix}")
    log_lines.append("observed_site_packages:")
    for p in site_paths:
        log_lines.append(f"  - {p}")

    prefix_size, prefix_warn = dir_size_bytes(env_prefix)
    site_entries = []
    site_total = 0
    warnings: List[str] = []
    warnings.extend(prefix_warn)
    for p in site_paths:
        size, warn = dir_size_bytes(p)
        warnings.extend(warn)
        site_total += int(size)
        site_entries.append({"path": str(p), "size_bytes": int(size)})

    observed = {
        "env_prefix": str(env_prefix),
        "env_prefix_size_MB": int(round(prefix_size / (1024 * 1024))),
        "site_packages": site_entries,
        "site_packages_total_bytes": int(site_total),
    }

    log_lines.append(f"env_prefix_size_bytes={prefix_size}")
    log_lines.append(f"site_packages_total_bytes={site_total}")
    if warnings:
        log_lines.append("warnings:")
        log_lines.extend([f"  - {w}" for w in warnings[:200]])

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    write_json(
        results_path,
        {
            "status": "success",
            "skip_reason": "not_applicable",
            "exit_code": 0,
            "stage": "env_size",
            "task": "measure",
            "command": f"python {Path(__file__).name} --report-path {report_path}",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "reported_python_path": python_exe,
            "observed": observed,
            "meta": {**meta, "warnings": warnings},
            "failure_category": "not_applicable",
            "error_excerpt": tail_lines(log_path),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
