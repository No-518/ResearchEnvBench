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


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _tail(path: Path, max_lines: int = 240) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def _resolve_report_path(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def _git_commit(repo: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo), stderr=subprocess.DEVNULL, text=True)
            .strip()
        )
    except Exception:
        return ""


def _dir_size_bytes(path: Path, warnings: list[str]) -> int:
    total = 0
    try:
        if not path.exists():
            return 0
        for root, dirs, files in os.walk(path, followlinks=False):
            # Avoid descending into obviously irrelevant cache/venv subtrees inside sys.prefix (rare, but safe).
            dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git"}]
            for name in files:
                fp = Path(root) / name
                try:
                    total += fp.stat().st_size
                except PermissionError:
                    warnings.append(f"PermissionError reading size: {fp}")
                except FileNotFoundError:
                    continue
                except OSError as e:
                    warnings.append(f"OSError reading size: {fp}: {e}")
    except PermissionError:
        warnings.append(f"PermissionError walking: {path}")
    except OSError as e:
        warnings.append(f"OSError walking: {path}: {e}")
    return total


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default="")
    args = parser.parse_args(argv)

    repo = _repo_root()
    out_dir = repo / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path or None)
    git_commit = _git_commit(repo)

    timeout_sec = 120
    result: dict[str, Any] = {
        "status": "failure",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": f"python measure_env_size.py --report-path {report_path}",
        "reported_python_path": "",
        "observed": {
            "env_prefix": "",
            "env_prefix_size_MB": 0,
            "site_packages": [],
            "site_packages_total_bytes": 0,
        },
        "meta": {
            "git_commit": git_commit,
            "timestamp_utc": _utc_timestamp(),
            "warnings": [],
        },
        "failure_category": "env_size_failed",
        "error_excerpt": "",
    }

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        python_path = report.get("python_path", "")
        if not isinstance(python_path, str) or not python_path.strip():
            raise RuntimeError("python_path missing in report.json")
        result["reported_python_path"] = python_path

        probe_code = r"""
import json
import site
import sys

env_prefix = sys.prefix
site_pkgs = []
try:
  site_pkgs.extend(site.getsitepackages() or [])
except Exception:
  pass
try:
  usp = site.getusersitepackages()
  if isinstance(usp, str) and usp:
    site_pkgs.append(usp)
except Exception:
  pass

print(json.dumps({
  "env_prefix": env_prefix,
  "site_packages": sorted({p for p in site_pkgs if isinstance(p, str)}),
}, ensure_ascii=False))
"""

        cmd = [python_path, "-c", probe_code]
        completed = subprocess.run(
            cmd,
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
        log_path.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8", errors="replace")
        if completed.returncode != 0:
            raise RuntimeError(f"Probe failed with rc={completed.returncode}")

        observed_prefix = json.loads((completed.stdout or "{}").strip() or "{}")
        env_prefix = Path(observed_prefix.get("env_prefix", ""))
        site_packages = [Path(p) for p in (observed_prefix.get("site_packages") or []) if isinstance(p, str)]

        warnings: list[str] = []
        env_prefix_bytes = _dir_size_bytes(env_prefix, warnings)
        site_entries = []
        site_total = 0
        for sp in site_packages:
            size = _dir_size_bytes(sp, warnings)
            site_entries.append({"path": str(sp), "size_bytes": size})
            site_total += size

        result["observed"] = {
            "env_prefix": str(env_prefix),
            "env_prefix_size_MB": int(round(env_prefix_bytes / (1024 * 1024))),
            "site_packages": site_entries,
            "site_packages_total_bytes": site_total,
        }
        result["meta"]["warnings"] = warnings

        result["status"] = "success"
        result["exit_code"] = 0
        result["failure_category"] = ""
        result["error_excerpt"] = ""
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    except Exception as e:
        if not log_path.exists():
            log_path.write_text("", encoding="utf-8")
        result["status"] = "failure"
        result["exit_code"] = 1
        result["failure_category"] = "env_size_failed"
        result["meta"]["exception"] = f"{type(e).__name__}: {e}"
        result["meta"]["traceback"] = traceback.format_exc(limit=60)
        result["error_excerpt"] = _tail(log_path)
        results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

