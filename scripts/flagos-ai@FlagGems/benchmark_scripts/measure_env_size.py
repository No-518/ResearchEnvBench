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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git_commit(repo_root: Path) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _default_report_path(cli: str | None) -> str:
    if cli:
        return cli
    return os.environ.get("SCIMLOPSBENCH_REPORT") or "/opt/scimlopsbench/report.json"


def _dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file(follow_symlinks=False):
                    try:
                        total += entry.stat(follow_symlinks=False).st_size
                    except Exception as e:
                        warnings.append(f"stat_failed:{entry.path}:{e}")
                elif entry.is_dir(follow_symlinks=False):
                    total += _dir_size_bytes(Path(entry.path), warnings)
            except PermissionError as e:
                warnings.append(f"permission_denied:{entry.path}:{e}")
            except FileNotFoundError:
                continue
    except PermissionError as e:
        warnings.append(f"permission_denied:{path}:{e}")
    except FileNotFoundError:
        return 0
    return total


def _python_query(python_path: str) -> Tuple[int, str]:
    code = r"""
import json, site, sys
payload = {
  "sys_prefix": sys.prefix,
  "site_packages": list(dict.fromkeys([p for p in site.getsitepackages() if isinstance(p, str)])),
  "user_site": site.getusersitepackages(),
}
print(json.dumps(payload))
"""
    p = subprocess.run(
        [python_path, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=60,
    )
    return p.returncode, p.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "env_size"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    report_path = _default_report_path(args.report_path)
    reported_python_path = ""
    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    error_excerpt = ""
    observed: Dict[str, Any] = {}
    warnings: List[str] = []

    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        reported_python_path = str(report.get("python_path", "") or "")
    except Exception as e:
        log(f"[env_size] Failed to read report.json: {report_path}: {e}")
        error_excerpt = f"Failed to read report.json: {report_path}: {e}"
        payload = {
            "status": status,
            "exit_code": exit_code,
            "stage": "env_size",
            "task": "measure",
            "command": f"{sys.executable} {Path(__file__).name} --report-path {report_path}",
            "reported_python_path": reported_python_path,
            "observed": observed,
            "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_timestamp(), "warnings": warnings},
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }
        results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    if not reported_python_path or not Path(reported_python_path).exists() or not os.access(reported_python_path, os.X_OK):
        log(f"[env_size] Invalid python_path in report: {reported_python_path}")
        error_excerpt = f"Invalid python_path in report: {reported_python_path}"
        payload = {
            "status": status,
            "exit_code": exit_code,
            "stage": "env_size",
            "task": "measure",
            "command": f"{sys.executable} {Path(__file__).name} --report-path {report_path}",
            "reported_python_path": reported_python_path,
            "observed": observed,
            "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_timestamp(), "warnings": warnings},
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }
        results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    log(f"[env_size] Using reported python_path: {reported_python_path}")
    rc, out = _python_query(reported_python_path)
    if rc != 0:
        log(f"[env_size] Failed to query env paths via reported python: rc={rc}\n{out}")
        error_excerpt = "\n".join(out.splitlines()[-220:])
        payload = {
            "status": status,
            "exit_code": exit_code,
            "stage": "env_size",
            "task": "measure",
            "command": f"{sys.executable} {Path(__file__).name} --report-path {report_path}",
            "reported_python_path": reported_python_path,
            "observed": observed,
            "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_timestamp(), "warnings": warnings},
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }
        results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    try:
        info = json.loads(out.strip().splitlines()[-1])
    except Exception as e:
        log(f"[env_size] Invalid JSON from python query: {e}\n{out}")
        error_excerpt = "\n".join(out.splitlines()[-220:])
        payload = {
            "status": status,
            "exit_code": exit_code,
            "stage": "env_size",
            "task": "measure",
            "command": f"{sys.executable} {Path(__file__).name} --report-path {report_path}",
            "reported_python_path": reported_python_path,
            "observed": observed,
            "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_timestamp(), "warnings": warnings},
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }
        results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    env_prefix = Path(info.get("sys_prefix", ""))
    site_packages = [Path(p) for p in (info.get("site_packages") or []) if isinstance(p, str)]
    user_site = info.get("user_site")
    if isinstance(user_site, str) and user_site:
        site_packages.append(Path(user_site))
    # Deduplicate
    seen = set()
    site_packages_uniq: List[Path] = []
    for p in site_packages:
        sp = str(p)
        if sp not in seen:
            seen.add(sp)
            site_packages_uniq.append(p)

    env_prefix_size = _dir_size_bytes(env_prefix, warnings) if env_prefix.exists() else 0
    site_entries: List[Dict[str, Any]] = []
    site_total = 0
    for sp in site_packages_uniq:
        size = _dir_size_bytes(sp, warnings) if sp.exists() else 0
        site_entries.append({"path": str(sp), "size_bytes": size})
        site_total += size

    observed = {
        "env_prefix": str(env_prefix),
        "env_prefix_size_MB": round(env_prefix_size / (1024 * 1024), 2),
        "site_packages": site_entries,
        "site_packages_total_bytes": site_total,
    }

    status = "success"
    exit_code = 0
    failure_category = "unknown"
    error_excerpt = ""

    payload = {
        "status": status,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"{sys.executable} {Path(__file__).name} --report-path {report_path}",
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_timestamp(), "warnings": warnings},
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }
    results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
