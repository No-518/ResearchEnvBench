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


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT, text=True, timeout=5)
            .strip()
        )
    except Exception:  # noqa: BLE001
        return ""


def tail(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:  # noqa: BLE001
        return ""


def report_path(cli_path: str | None) -> str:
    if cli_path:
        return cli_path
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return env
    return DEFAULT_REPORT_PATH


def dir_size_bytes(path: Path) -> Tuple[int, List[str]]:
    total = 0
    warnings: List[str] = []

    def walk(p: Path) -> None:
        nonlocal total
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            walk(Path(entry.path))
                    except PermissionError as exc:
                        warnings.append(f"PermissionError: {entry.path}: {exc}")
                    except FileNotFoundError:
                        continue
        except PermissionError as exc:
            warnings.append(f"PermissionError: {p}: {exc}")
        except FileNotFoundError:
            return

    walk(path)
    return total, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Python environment size from report.json python_path.")
    parser.add_argument("--report-path", default="", help="Override report path.")
    args = parser.parse_args()

    root = repo_root()
    stage_dir = root / "build_output" / "env_size"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    rp = Path(report_path(args.report_path or None))
    reported_python_path = ""

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    error_excerpt = ""
    observed: Dict[str, Any] = {}
    warnings: List[str] = []

    try:
        report = json.loads(rp.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("report.json is not an object")
        reported_python_path = str(report.get("python_path", ""))
        if not reported_python_path:
            raise ValueError("report.json missing python_path")
        py = Path(reported_python_path)
        if not (py.is_file() and os.access(py, os.X_OK)):
            raise ValueError(f"python_path is not executable: {reported_python_path}")

        probe = subprocess.check_output(
            [
                reported_python_path,
                "-c",
                "import json,sys,site; print(json.dumps({'sys_prefix': sys.prefix, 'site_packages': site.getsitepackages(), 'user_site': site.getusersitepackages()}))",
            ],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        ).strip()
        info = json.loads(probe)
        env_prefix = Path(info["sys_prefix"])
        site_packages = []
        if isinstance(info.get("site_packages"), list):
            site_packages.extend([str(p) for p in info["site_packages"]])
        user_site = info.get("user_site")
        if isinstance(user_site, str) and user_site:
            site_packages.append(user_site)
        # de-dup while preserving order
        seen = set()
        site_packages = [p for p in site_packages if not (p in seen or seen.add(p))]

        env_bytes, env_w = dir_size_bytes(env_prefix)
        warnings.extend(env_w)

        sp_records = []
        sp_total = 0
        for sp in site_packages:
            p = Path(sp)
            if not p.exists():
                warnings.append(f"site-packages path does not exist: {sp}")
                continue
            sz, w = dir_size_bytes(p)
            warnings.extend(w)
            sp_records.append({"path": sp, "size_bytes": sz})
            sp_total += sz

        observed = {
            "env_prefix": str(env_prefix),
            "env_prefix_size_MB": int(round(env_bytes / (1024 * 1024))),
            "site_packages": sp_records,
            "site_packages_total_bytes": sp_total,
        }

        status = "success"
        exit_code = 0
        failure_category = "unknown"
        log_path.write_text(
            json.dumps({"reported_python_path": reported_python_path, "observed": observed, "warnings": warnings}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )

    except Exception as exc:  # noqa: BLE001
        log_path.write_text(f"env_size failed: {exc}\n", encoding="utf-8")
        error_excerpt = tail(log_path)
        status = "failure"
        exit_code = 1
        failure_category = "env_size_failed"

    payload: Dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"benchmark_scripts/measure_env_size.py --report-path {str(rp)}",
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "git_commit": git_commit(root),
            "timestamp_utc": utc_timestamp(),
            "warnings": warnings,
            "report_path": str(rp),
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

