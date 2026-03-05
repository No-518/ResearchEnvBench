#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_report_path(cli_path: Optional[str]) -> Path:
    if cli_path:
        return Path(cli_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def safe_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def tail_excerpt(path: Path, max_lines: int = 220) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:])
    except Exception:
        return ""


def is_executable(path: Path) -> bool:
    return path.is_file() and os.access(str(path), os.X_OK)


def load_report(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"report missing: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json: {path}: {e}"
    except Exception as e:
        return None, f"failed reading report: {path}: {e}"


@dataclass
class SizeResult:
    size_bytes: int
    warnings: List[str]


def dir_size_bytes(path: Path) -> SizeResult:
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
                        if entry.is_dir(follow_symlinks=False):
                            walk(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                total += entry.stat(follow_symlinks=False).st_size
                            except PermissionError as e:
                                warnings.append(f"permission denied: {entry.path}: {e}")
                            except FileNotFoundError:
                                continue
                    except PermissionError as e:
                        warnings.append(f"permission denied: {entry.path}: {e}")
                    except FileNotFoundError:
                        continue
        except PermissionError as e:
            warnings.append(f"permission denied: {p}: {e}")
        except FileNotFoundError:
            return

    walk(path)
    return SizeResult(total, warnings)


def query_env_paths(python_exe: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    code = r"""
import json, os, site, sys
payload = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_getsitepackages": [],
  "site_getusersitepackages": None,
}
try:
  payload["site_getsitepackages"] = site.getsitepackages()
except Exception:
  payload["site_getsitepackages"] = []
try:
  payload["site_getusersitepackages"] = site.getusersitepackages()
except Exception:
  payload["site_getusersitepackages"] = None
print(json.dumps(payload))
"""
    try:
        p = subprocess.run(
            [python_exe, "-c", code],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except Exception as e:
        return None, f"failed to query env paths: {e}"
    if p.returncode != 0:
        return None, f"python query failed (rc={p.returncode}): {p.stderr.strip()}"
    try:
        return json.loads(p.stdout), None
    except Exception as e:
        return None, f"failed to parse python query output: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None, help="Override report path (highest priority)")
    args = ap.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    started = datetime.now(tz=timezone.utc)
    report_path = resolve_report_path(args.report_path)

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    observed: Dict[str, Any] = {}
    meta: Dict[str, Any] = {"timestamp_utc": started.isoformat()}

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write("== env_size stage ==\n")
        log_f.write(f"report_path: {report_path}\n")

        report, rep_err = load_report(report_path)
        if report is None:
            log_f.write((rep_err or "missing report") + "\n")
            meta["report_error"] = rep_err or "missing report"
            failure_category = "env_size_failed"
        else:
            python_path = report.get("python_path")
            meta["reported_python_path"] = python_path
            if not isinstance(python_path, str) or not python_path:
                log_f.write("python_path missing in report\n")
                failure_category = "env_size_failed"
            else:
                py = Path(python_path)
                if not is_executable(py):
                    log_f.write(f"python_path not executable: {python_path}\n")
                    failure_category = "env_size_failed"
                else:
                    info, qerr = query_env_paths(python_path)
                    if info is None:
                        log_f.write((qerr or "failed to query env paths") + "\n")
                        failure_category = "env_size_failed"
                    else:
                        env_prefix = Path(info.get("sys_prefix", ""))
                        site_paths: List[str] = []
                        for pth in info.get("site_getsitepackages", []) or []:
                            if isinstance(pth, str):
                                site_paths.append(pth)
                        usp = info.get("site_getusersitepackages")
                        if isinstance(usp, str):
                            site_paths.append(usp)
                        site_paths = [p for p in dict.fromkeys(site_paths) if p]  # stable unique

                        observed["env_prefix"] = str(env_prefix)

                        prefix_size = dir_size_bytes(env_prefix) if env_prefix.exists() else SizeResult(0, [f"env_prefix not found: {env_prefix}"])
                        observed["env_prefix_size_bytes"] = int(prefix_size.size_bytes)
                        observed["env_prefix_size_MB"] = int(round(prefix_size.size_bytes / (1024 * 1024)))

                        site_entries: List[Dict[str, Any]] = []
                        total_site = 0
                        warnings: List[str] = []
                        warnings.extend(prefix_size.warnings)

                        for sp in site_paths:
                            spp = Path(sp)
                            if not spp.exists():
                                warnings.append(f"site-packages path not found: {sp}")
                                continue
                            sr = dir_size_bytes(spp)
                            site_entries.append({"path": str(spp), "size_bytes": int(sr.size_bytes)})
                            total_site += int(sr.size_bytes)
                            warnings.extend(sr.warnings)

                        observed["site_packages"] = site_entries
                        observed["site_packages_total_bytes"] = int(total_site)
                        meta["warnings"] = warnings

                        status = "success"
                        exit_code = 0
                        failure_category = "unknown"

    ended = datetime.now(tz=timezone.utc)
    meta["start_time_utc"] = started.isoformat()
    meta["end_time_utc"] = ended.isoformat()
    meta["duration_sec"] = max(0.0, (ended - started).total_seconds())

    payload: Dict[str, Any] = {
        "status": status,
        "exit_code": int(exit_code),
        "stage": "env_size",
        "task": "measure",
        "command": f"{sys.executable} benchmark_scripts/measure_env_size.py --report-path {str(report_path)}",
        "reported_python_path": meta.get("reported_python_path", ""),
        "observed": observed,
        "meta": meta,
        "failure_category": failure_category,
        "error_excerpt": tail_excerpt(log_path),
    }

    safe_write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        root = repo_root()
        out_dir = root / "build_output" / "env_size"
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "log.txt"
        results_path = out_dir / "results.json"
        with log_path.open("a", encoding="utf-8") as f:
            f.write("fatal exception\n")
            f.write(traceback.format_exc() + "\n")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"{sys.executable} benchmark_scripts/measure_env_size.py",
            "reported_python_path": "",
            "observed": {},
            "meta": {"timestamp_utc": datetime.now(tz=timezone.utc).isoformat()},
            "failure_category": "env_size_failed",
            "error_excerpt": tail_excerpt(log_path),
        }
        safe_write_json(results_path, payload)
        raise SystemExit(1)
