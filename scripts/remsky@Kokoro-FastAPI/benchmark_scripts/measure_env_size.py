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
    return Path(__file__).resolve().parents[1]


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_report_path(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def get_git_commit(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False):
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except PermissionError as e:
                            warnings.append(f"PermissionError stat file: {entry.path}: {e}")
                        except FileNotFoundError:
                            continue
                    elif entry.is_dir(follow_symlinks=False):
                        total += dir_size_bytes(Path(entry.path), warnings)
                except PermissionError as e:
                    warnings.append(f"PermissionError scandir entry: {entry.path}: {e}")
                except FileNotFoundError:
                    continue
    except PermissionError as e:
        warnings.append(f"PermissionError scandir dir: {path}: {e}")
    except FileNotFoundError:
        warnings.append(f"Missing path: {path}")
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

    logs: List[str] = []

    def log(msg: str) -> None:
        logs.append(msg)
        print(msg)

    report_path = resolve_report_path(args.report_path)
    reported_python_path = ""
    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    observed: Dict[str, Any] = {}
    warnings: List[str] = []
    timeout_sec = 120

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("report_root_not_object")
        reported_python_path = str(report.get("python_path") or "")
    except Exception as e:
        log(f"Failed to read report.json: {report_path}: {e}")
        reported_python_path = ""

    if not reported_python_path:
        log("python_path missing in report.json")
    else:
        python_path = Path(reported_python_path)
        if not python_path.exists():
            log(f"python_path does not exist: {reported_python_path}")
        elif not os.access(reported_python_path, os.X_OK):
            log(f"python_path not executable: {reported_python_path}")
        else:
            # Probe the environment paths using the reported interpreter.
            try:
                probe = subprocess.check_output(
                    [
                        reported_python_path,
                        "-c",
                        (
                            "import json, sys, site; "
                            "payload={'sys_prefix': sys.prefix, "
                            "'site_packages': site.getsitepackages() if hasattr(site,'getsitepackages') else [], "
                            "'user_site': site.getusersitepackages() if hasattr(site,'getusersitepackages') else ''}; "
                            "print(json.dumps(payload))"
                        ),
                    ],
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=60,
                )
                info = json.loads(probe.strip() or "{}")
                env_prefix = Path(str(info.get("sys_prefix") or ""))
                site_packages_paths = [Path(p) for p in (info.get("site_packages") or []) if isinstance(p, str)]
                user_site = info.get("user_site")
                if isinstance(user_site, str) and user_site:
                    site_packages_paths.append(Path(user_site))

                log(f"Observed env_prefix={env_prefix}")
                env_prefix_bytes = dir_size_bytes(env_prefix, warnings)
                site_sizes: List[Dict[str, Any]] = []
                total_site_bytes = 0
                for sp in site_packages_paths:
                    size = dir_size_bytes(sp, warnings)
                    site_sizes.append({"path": str(sp), "size_bytes": size})
                    total_site_bytes += size

                observed = {
                    "env_prefix": str(env_prefix),
                    "env_prefix_size_MB": int(round(env_prefix_bytes / (1024 * 1024))),
                    "site_packages": site_sizes,
                    "site_packages_total_bytes": total_site_bytes,
                }

                status = "success"
                exit_code = 0
                failure_category = ""
            except Exception as e:
                log(f"Failed to probe/measure environment via python_path: {e}")

    env_vars = {
        "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
        "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
    }

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"{sys.executable} {Path(__file__).name} --report-path {report_path}",
        "timeout_sec": timeout_sec,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "python": sys.executable,
            "git_commit": get_git_commit(root),
            "env_vars": env_vars,
            "decision_reason": "Measure sys.prefix and site-packages sizes by probing the report's python_path.",
            "timestamp_utc": utc_timestamp(),
            "warnings": warnings,
        },
        "failure_category": failure_category,
        "error_excerpt": "\n".join((logs + warnings)[-200:]),
    }

    log_path.write_text("\n".join(logs) + ("\n" if logs else ""), encoding="utf-8")
    write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
