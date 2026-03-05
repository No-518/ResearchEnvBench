#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail(path: Path, n: int = 240) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:
        return None, f"invalid_json: {e}"


def resolve_report_path(cli: str) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT", "").strip()
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def safe_is_executable(path: Path) -> bool:
    try:
        return path.exists() and os.access(str(path), os.X_OK) and path.is_file()
    except Exception:
        return False


def dir_size_bytes(root: Path, warnings: List[str]) -> int:
    total = 0
    stack = [root]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except PermissionError:
                        warnings.append(f"permission_denied: {entry.path}")
                        continue
                    except FileNotFoundError:
                        continue
                    except Exception as e:
                        warnings.append(f"stat_error: {entry.path}: {type(e).__name__}: {e}")
                        continue

                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    else:
                        total += int(getattr(st, "st_size", 0) or 0)
        except PermissionError:
            warnings.append(f"permission_denied: {p}")
        except FileNotFoundError:
            continue
        except Exception as e:
            warnings.append(f"scandir_error: {p}: {type(e).__name__}: {e}")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure environment size for agent python.")
    ap.add_argument("--report-path", default="", help="Override report.json path")
    args = ap.parse_args()

    root = repo_root()
    stage_dir = root / "build_output" / "env_size"
    ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    report_path = resolve_report_path(args.report_path)
    report, err = read_json(report_path)

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    command = f"python {Path(__file__).name} --report-path {report_path}"

    payload: Dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": command,
        "reported_python_path": "",
        "observed": {
            "env_prefix": "",
            "env_prefix_size_MB": 0,
            "site_packages": [],
            "site_packages_total_bytes": 0,
        },
        "meta": {"timestamp_utc": utc_ts(), "warnings": [], "report_path": str(report_path)},
        "failure_category": failure_category,
        "error_excerpt": "",
    }

    if report is None:
        log_path.write_text(f"[env_size] report read failed: {err} ({report_path})\n", encoding="utf-8")
        payload["failure_category"] = "env_size_failed"
        payload["error_excerpt"] = tail(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    python_path = str(report.get("python_path") or "").strip()
    payload["reported_python_path"] = python_path
    if not python_path:
        log_path.write_text("[env_size] python_path missing in report.json\n", encoding="utf-8")
        payload["error_excerpt"] = tail(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    py = Path(python_path)
    if not safe_is_executable(py):
        log_path.write_text(f"[env_size] python_path not executable: {python_path}\n", encoding="utf-8")
        payload["error_excerpt"] = tail(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    probe_code = r"""
import json, site, sys
out = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_packages": list(site.getsitepackages()) if hasattr(site, "getsitepackages") else [],
  "user_site": site.getusersitepackages() if hasattr(site, "getusersitepackages") else "",
}
print(json.dumps(out))
"""

    with log_path.open("a", encoding="utf-8") as log_fp:
        log_fp.write(f"[env_size] report_path={report_path}\n")
        log_fp.write(f"[env_size] python_path={python_path}\n")

    try:
        out = subprocess.check_output([python_path, "-c", probe_code], stderr=subprocess.STDOUT, text=True, timeout=60)
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(out)
        probe = json.loads(out.strip())
    except Exception as e:
        with log_path.open("a", encoding="utf-8") as log_fp:
            log_fp.write(f"[env_size] probe failed: {type(e).__name__}: {e}\n")
        payload["error_excerpt"] = tail(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    env_prefix = Path(str(probe.get("sys_prefix") or "")).resolve()
    site_packages_paths: List[Path] = []
    for p in probe.get("site_packages") or []:
        try:
            site_packages_paths.append(Path(str(p)).resolve())
        except Exception:
            continue
    user_site = str(probe.get("user_site") or "").strip()
    if user_site:
        try:
            site_packages_paths.append(Path(user_site).resolve())
        except Exception:
            pass

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique_site: List[Path] = []
    for p in site_packages_paths:
        s = str(p)
        if s not in seen:
            seen.add(s)
            unique_site.append(p)

    warnings: List[str] = []
    env_bytes = 0
    if env_prefix.exists():
        env_bytes = dir_size_bytes(env_prefix, warnings)
    else:
        warnings.append(f"env_prefix_missing: {env_prefix}")

    site_entries: List[Dict[str, Any]] = []
    site_total = 0
    for sp in unique_site:
        if not sp.exists():
            warnings.append(f"site_packages_missing: {sp}")
            continue
        sz = dir_size_bytes(sp, warnings)
        site_entries.append({"path": str(sp), "size_bytes": int(sz)})
        site_total += int(sz)

    payload["status"] = "success"
    payload["exit_code"] = 0
    payload["failure_category"] = "none"
    payload["observed"] = {
        "env_prefix": str(env_prefix),
        "env_prefix_size_MB": int(round(env_bytes / (1024 * 1024))),
        "site_packages": site_entries,
        "site_packages_total_bytes": int(site_total),
    }
    payload["meta"] = {
        "timestamp_utc": utc_ts(),
        "warnings": warnings,
        "report_path": str(report_path),
        "probe": probe,
    }
    payload["error_excerpt"] = ""

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

