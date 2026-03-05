#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import site
import subprocess
import sys
from typing import Any


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def tail_text(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_size = min(size, 512 * 1024)
            if read_size > 0:
                f.seek(-read_size, os.SEEK_END)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-max_lines:])
    except Exception:
        return ""


def write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_dir_size_bytes(path: pathlib.Path, warnings: list[str]) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path, onerror=None, followlinks=False):
            for name in files:
                fp = pathlib.Path(root) / name
                try:
                    total += fp.stat().st_size
                except PermissionError:
                    warnings.append(f"permission denied: {fp}")
                except FileNotFoundError:
                    continue
                except OSError as e:
                    warnings.append(f"oserror: {fp}: {e}")
    except PermissionError:
        warnings.append(f"permission denied walking: {path}")
    except OSError as e:
        warnings.append(f"oserror walking: {path}: {e}")
    return total


def resolve_report_path(cli: str | None) -> pathlib.Path:
    if cli:
        return pathlib.Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return pathlib.Path(env)
    return pathlib.Path("/opt/scimlopsbench/report.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    reported_python_path = ""
    exit_code = 1
    status = "failure"
    failure_category = "env_size_failed"

    assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

    observed: dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": None,
        "site_packages": [],
        "site_packages_total_bytes": None,
    }
    warnings: list[str] = []

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[env_size] report_path={report_path}\n")

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        reported_python_path = str(report.get("python_path") or "")
        if not reported_python_path:
            raise RuntimeError("python_path missing in report.json")

        # Probe env prefix + site-packages paths using the reported python
        probe_code = r"""
import json, site, sys
out = {
  "executable": sys.executable,
  "prefix": sys.prefix,
  "site_packages": list(site.getsitepackages()) if hasattr(site, "getsitepackages") else [],
  "user_site": site.getusersitepackages() if hasattr(site, "getusersitepackages") else None,
}
print(json.dumps(out))
"""
        out = subprocess.check_output(
            [reported_python_path, "-c", probe_code],
            cwd=str(root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        ).strip()
        probe = json.loads(out)
        env_prefix = pathlib.Path(probe["prefix"])
        observed["env_prefix"] = str(env_prefix)

        env_prefix_bytes = safe_dir_size_bytes(env_prefix, warnings)
        observed["env_prefix_size_MB"] = int(env_prefix_bytes / (1024 * 1024))

        site_paths: list[str] = []
        for p in probe.get("site_packages") or []:
            if p:
                site_paths.append(str(p))
        user_site = probe.get("user_site")
        if user_site:
            site_paths.append(str(user_site))

        seen = set()
        site_entries = []
        total_site = 0
        for p in site_paths:
            if p in seen:
                continue
            seen.add(p)
            pp = pathlib.Path(p)
            if not pp.exists():
                continue
            sz = safe_dir_size_bytes(pp, warnings)
            site_entries.append({"path": str(pp), "size_bytes": sz})
            total_site += sz

        observed["site_packages"] = site_entries
        observed["site_packages_total_bytes"] = total_site

        status = "success"
        exit_code = 0
        failure_category = "unknown"

    except Exception as e:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[env_size] ERROR: {e}\n")

    if warnings:
        with log_path.open("a", encoding="utf-8") as log:
            for w in warnings[:200]:
                log.write(f"[env_size] warning: {w}\n")

    results = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"{sys.executable} {pathlib.Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": assets,
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "report_path": str(report_path),
            "warnings_count": len(warnings),
        },
        "failure_category": failure_category if status == "failure" else "unknown",
        "error_excerpt": tail_text(log_path),
    }
    write_json(results_path, results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
