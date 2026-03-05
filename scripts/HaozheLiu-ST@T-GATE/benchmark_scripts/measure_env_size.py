#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_report_path(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def dir_size_bytes(root: Path, warnings: list[str]) -> int:
    total = 0
    try:
        for base, dirs, files in os.walk(root, followlinks=False):
            for name in files:
                fp = Path(base) / name
                try:
                    total += fp.stat().st_size
                except PermissionError:
                    warnings.append(f"permission_denied: {fp}")
                except FileNotFoundError:
                    continue
                except OSError as e:
                    warnings.append(f"oserror: {fp}: {e}")
    except Exception as e:
        warnings.append(f"walk_failed: {root}: {e}")
    return total


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

    result: dict[str, Any] = {
        "status": "failure",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": "benchmark_scripts/measure_env_size.py",
        "reported_python_path": "",
        "observed": {},
        "meta": {"timestamp_utc": utc_now_iso(), "git_commit": "", "warnings": [], "report_path": str(report_path)},
        "failure_category": "env_size_failed",
        "error_excerpt": "",
    }

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("report_json_not_object")
        python_path = report.get("python_path")
        result["reported_python_path"] = str(python_path or "")
        if not isinstance(python_path, str) or not python_path:
            raise RuntimeError("python_path missing in report")
        if not (os.path.isfile(python_path) and os.access(python_path, os.X_OK)):
            raise RuntimeError(f"python_path not executable: {python_path}")

        probe = r"""
import json, sys
try:
    import site
except Exception:
    site = None

out = {"sys_prefix": sys.prefix, "site_packages": [], "user_site": ""}
if site is not None:
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
        with log_path.open("w", encoding="utf-8", errors="replace") as lf:
            lf.write(f"[env_size] report_path={report_path}\n")
            lf.write(f"[env_size] python_path={python_path}\n")
            lf.write(f"[env_size] timestamp_utc={utc_now_iso()}\n")
            lf.flush()
            p = subprocess.run(
                [python_path, "-c", probe],
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
            )
            lf.write(p.stdout)
            lf.write(f"\n[env_size] probe_returncode={p.returncode}\n")

        observed_probe: dict[str, Any] = json.loads(p.stdout.strip().splitlines()[-1])
        env_prefix = Path(str(observed_probe.get("sys_prefix", ""))).resolve()
        site_paths_raw = list(observed_probe.get("site_packages") or [])
        user_site = str(observed_probe.get("user_site") or "")
        if user_site:
            site_paths_raw.append(user_site)

        warnings: list[str] = []
        env_prefix_size = dir_size_bytes(env_prefix, warnings)
        site_packages: list[dict[str, Any]] = []
        site_total = 0
        for sp in sorted({str(Path(p).resolve()) for p in site_paths_raw if p}):
            sp_path = Path(sp)
            if not sp_path.exists():
                continue
            size = dir_size_bytes(sp_path, warnings)
            site_packages.append({"path": str(sp_path), "size_bytes": int(size)})
            site_total += int(size)

        result["observed"] = {
            "env_prefix": str(env_prefix),
            "env_prefix_size_MB": round(env_prefix_size / (1024 * 1024), 2),
            "site_packages": site_packages,
            "site_packages_total_bytes": int(site_total),
        }
        result["meta"]["warnings"] = warnings
        result["status"] = "success"
        result["exit_code"] = 0
        result["failure_category"] = ""
        result["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except Exception as e:
        with log_path.open("a", encoding="utf-8", errors="replace") as lf:
            lf.write("\n[env_size] exception:\n")
            lf.write(str(e) + "\n")
            lf.write(traceback.format_exc())
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = "env_size_failed"
        result["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

