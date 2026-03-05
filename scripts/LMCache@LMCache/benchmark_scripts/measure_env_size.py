#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:  # noqa: BLE001
        return None, f"invalid_json: {e}"


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def tail(path: Path, max_lines: int = 220) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def is_executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except Exception:
        return False


def dir_size_bytes(root: Path) -> Tuple[int, List[str]]:
    total = 0
    warnings: List[str] = []
    stack = [root]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            try:
                                total += entry.stat(follow_symlinks=False).st_size
                            except PermissionError:
                                warnings.append(f"permission_denied:file:{entry.path}")
                            except FileNotFoundError:
                                continue
                    except PermissionError:
                        warnings.append(f"permission_denied:entry:{entry.path}")
                    except FileNotFoundError:
                        continue
        except PermissionError:
            warnings.append(f"permission_denied:dir:{current}")
        except FileNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001
            warnings.append(f"error:{current}:{e}")

    return total, warnings


PROBE_SNIPPET = r"""
import json
import site
import sys

out = {
  "python_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "base_prefix": getattr(sys, "base_prefix", ""),
  "site_packages": site.getsitepackages() if hasattr(site, "getsitepackages") else [],
  "user_site": site.getusersitepackages() if hasattr(site, "getusersitepackages") else "",
}
print(json.dumps(out, ensure_ascii=False))
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Measure environment size for the agent-reported Python")
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args(argv)

    root = repo_root()
    out_dir = root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report, report_err = read_json(report_path)

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    reported_python_path = ""
    observed: Dict[str, Any] = {}
    meta: Dict[str, Any] = {
        "timestamp_utc": utc_timestamp(),
        "report_path": str(report_path),
        "warnings": [],
    }

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[env_size] start_utc={utc_timestamp()}\n")
        log_f.write(f"[env_size] report_path={report_path}\n")

        if report is None:
            log_f.write(f"[env_size] ERROR: report missing/invalid: {report_err}\n")
            failure_category = "env_size_failed"
        else:
            reported_python_path = str(report.get("python_path", "") or "")
            if not reported_python_path:
                log_f.write("[env_size] ERROR: python_path missing in report\n")
                failure_category = "env_size_failed"
            else:
                py_path = Path(reported_python_path)
                if not is_executable(py_path):
                    log_f.write(f"[env_size] ERROR: python_path not executable: {py_path}\n")
                    failure_category = "env_size_failed"
                else:
                    cmd = [reported_python_path, "-c", PROBE_SNIPPET]
                    cmd_str = " ".join(shlex.quote(x) for x in cmd)
                    log_f.write(f"[env_size] probe_command={cmd_str}\n")
                    try:
                        proc = subprocess.run(
                            cmd,
                            cwd=str(root),
                            capture_output=True,
                            text=True,
                            timeout=60,
                        )
                    except subprocess.TimeoutExpired:
                        log_f.write("[env_size] ERROR: probe timeout\n")
                        failure_category = "env_size_failed"
                    else:
                        log_f.write("[env_size] --- probe stdout ---\n")
                        log_f.write(proc.stdout + "\n")
                        log_f.write("[env_size] --- probe stderr ---\n")
                        log_f.write(proc.stderr + "\n")

                        try:
                            probe = json.loads(proc.stdout) if proc.stdout.strip() else {}
                        except Exception as e:  # noqa: BLE001
                            log_f.write(f"[env_size] ERROR: invalid probe JSON: {e}\n")
                            failure_category = "env_size_failed"
                        else:
                            env_prefix = str(probe.get("sys_prefix", "") or "")
                            site_packages = probe.get("site_packages", []) or []
                            user_site = probe.get("user_site", "") or ""

                            observed["env_prefix"] = env_prefix
                            observed["python_executable"] = str(probe.get("python_executable", "") or "")
                            observed["site_packages"] = []
                            observed["user_site"] = user_site

                            total_warnings: List[str] = []

                            env_prefix_size = 0
                            if env_prefix:
                                size, warns = dir_size_bytes(Path(env_prefix))
                                env_prefix_size = size
                                total_warnings.extend(warns)

                            observed["env_prefix_size_MB"] = int(env_prefix_size / (1024 * 1024))

                            site_total = 0
                            for sp in site_packages:
                                sp_path = str(sp)
                                size = 0
                                warns: List[str] = []
                                if sp_path and Path(sp_path).exists():
                                    size, warns = dir_size_bytes(Path(sp_path))
                                site_total += size
                                total_warnings.extend(warns)
                                observed["site_packages"].append({"path": sp_path, "size_bytes": size})

                            observed["site_packages_total_bytes"] = site_total
                            meta["warnings"] = total_warnings[:500]

                            status = "success"
                            exit_code = 0
                            failure_category = "unknown"

    payload: Dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"python3 benchmark_scripts/measure_env_size.py --report-path {shlex.quote(str(report_path))}"
        if args.report_path
        else "python3 benchmark_scripts/measure_env_size.py",
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": meta,
        "failure_category": failure_category,
        "error_excerpt": tail(log_path),
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

