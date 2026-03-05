#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import site
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def stage_dir() -> Path:
    return repo_root() / "build_output" / "env_size"


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def resolve_report_path(cli_path: Optional[str]) -> str:
    if cli_path:
        return cli_path
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return os.environ["SCIMLOPSBENCH_REPORT"]
    return DEFAULT_REPORT_PATH


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def is_executable(path: Path) -> bool:
    return path.exists() and os.access(str(path), os.X_OK)


def get_env_info(python_exe: str) -> Dict[str, Any]:
    code = r"""
import json, os, site, sys
out = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_packages": [],
  "user_site": None,
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
    proc = subprocess.run([python_exe, "-c", code], capture_output=True, text=True, cwd=str(repo_root()))
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"python probe failed rc={proc.returncode}")
    return json.loads(proc.stdout.strip() or "{}")


def dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size
        for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
            # Skip obvious transient dirs inside env prefix if present.
            dirs[:] = [d for d in dirs if d not in {"__pycache__", ".mypy_cache", ".pytest_cache"}]
            for name in files:
                fp = Path(root) / name
                try:
                    total += fp.stat().st_size
                except PermissionError:
                    warnings.append(f"permission_error: {fp}")
                except FileNotFoundError:
                    continue
                except OSError as e:
                    warnings.append(f"os_error: {fp}: {e}")
    except PermissionError:
        warnings.append(f"permission_error: {path}")
    except FileNotFoundError:
        warnings.append(f"not_found: {path}")
    except OSError as e:
        warnings.append(f"os_error: {path}: {e}")
    return total


def mb(n_bytes: int) -> int:
    return int(round(n_bytes / (1024 * 1024)))


def tail_text(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:]).strip()
    except Exception as e:
        return f"[env_size] failed to read log: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure environment size for the benchmarked python.")
    parser.add_argument("--report-path", default=None, help="Override report path.")
    args = parser.parse_args()

    out_dir = stage_dir()
    safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)

    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": f"python benchmark_scripts/measure_env_size.py --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "reported_python_path": "",
        "observed": {
            "env_prefix": "",
            "env_prefix_size_MB": 0,
            "site_packages": [],
            "site_packages_total_bytes": 0,
        },
        "meta": {
            "timestamp_utc": utc_now_iso(),
            "report_path": report_path,
            "env_vars": {k: v for k, v in os.environ.items() if k.startswith("SCIMLOPSBENCH_")},
            "warnings": [],
        },
        "failure_category": "env_size_failed",
        "error_excerpt": "",
    }

    log_lines: List[str] = []
    log_lines.append(f"[env_size] timestamp_utc={utc_now_iso()}")
    log_lines.append(f"[env_size] report_path={report_path}")

    rp = Path(report_path)
    if not rp.exists():
        msg = f"report not found: {report_path}"
        log_lines.append(f"[env_size] ERROR: {msg}")
        results.update({"failure_category": "missing_report", "error_excerpt": msg})
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    try:
        report = load_json(rp)
    except Exception as e:
        msg = f"invalid report json: {e}"
        log_lines.append(f"[env_size] ERROR: {msg}")
        results.update({"failure_category": "missing_report", "error_excerpt": msg})
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        msg = "report missing python_path"
        log_lines.append(f"[env_size] ERROR: {msg}")
        results.update({"failure_category": "env_size_failed", "error_excerpt": msg})
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    results["reported_python_path"] = python_path
    log_lines.append(f"[env_size] python_path={python_path}")

    if not is_executable(Path(python_path)):
        msg = f"python_path not executable: {python_path}"
        log_lines.append(f"[env_size] ERROR: {msg}")
        results.update({"failure_category": "env_size_failed", "error_excerpt": msg})
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    try:
        info = get_env_info(python_path)
    except Exception as e:
        msg = str(e)
        log_lines.append(f"[env_size] ERROR: failed to probe env paths: {msg}")
        results.update({"failure_category": "env_size_failed", "error_excerpt": msg})
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    env_prefix = Path(str(info.get("sys_prefix", "")))
    site_paths: List[str] = []
    for p in info.get("site_packages") or []:
        if isinstance(p, str) and p:
            site_paths.append(p)
    user_site = info.get("user_site")
    if isinstance(user_site, str) and user_site:
        site_paths.append(user_site)

    warnings: List[str] = []
    env_prefix_size = dir_size_bytes(env_prefix, warnings) if env_prefix else 0

    site_entries: List[Dict[str, Any]] = []
    site_total = 0
    for sp in site_paths:
        p = Path(sp)
        size = dir_size_bytes(p, warnings) if p.exists() else 0
        site_entries.append({"path": sp, "size_bytes": size})
        site_total += size

    results["observed"] = {
        "env_prefix": str(env_prefix),
        "env_prefix_size_MB": mb(env_prefix_size),
        "site_packages": site_entries,
        "site_packages_total_bytes": site_total,
    }
    results["meta"]["warnings"] = warnings
    results.update({"status": "success", "exit_code": 0, "failure_category": "unknown", "error_excerpt": ""})

    log_lines.append(f"[env_size] env_prefix_size_bytes={env_prefix_size}")
    log_lines.append(f"[env_size] site_packages_total_bytes={site_total}")
    if warnings:
        log_lines.append(f"[env_size] warnings_count={len(warnings)}")

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
