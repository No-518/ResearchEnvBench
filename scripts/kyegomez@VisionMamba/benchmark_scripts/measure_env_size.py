#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(DEFAULT_REPORT_PATH)


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, stderr=subprocess.DEVNULL)
            .strip()
        )
    except Exception:
        return ""


def tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:]).strip()
    except Exception:
        return ""


def dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
            for name in files:
                fp = Path(root) / name
                try:
                    st = fp.stat()
                    if stat.S_ISREG(st.st_mode):
                        total += int(st.st_size)
                except PermissionError:
                    warnings.append(f"permission_denied: {fp}")
                except FileNotFoundError:
                    continue
                except OSError as e:
                    warnings.append(f"os_error: {fp}: {e}")
    except PermissionError:
        warnings.append(f"permission_denied: {path}")
    except FileNotFoundError:
        warnings.append(f"not_found: {path}")
    except OSError as e:
        warnings.append(f"os_error_walk: {path}: {e}")
    return total


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    root = repo_root()
    stage_dir = root / "build_output" / "env_size"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    reported_python_path = ""
    observed: Dict[str, Any] = {}
    warnings: List[str] = []

    with log_path.open("w", encoding="utf-8") as log_f:
        def log(msg: str) -> None:
            log_f.write(msg.rstrip() + "\n")
            log_f.flush()

        report_path = resolve_report_path(args.report_path)
        log(f"[env_size] repo_root={root}")
        log(f"[env_size] report_path={report_path}")

        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            failure_category = "missing_report"
            log("[env_size] ERROR: report.json missing")
            report = None
        except json.JSONDecodeError as e:
            failure_category = "invalid_json"
            log(f"[env_size] ERROR: invalid report.json: {e}")
            report = None
        except Exception as e:
            failure_category = "missing_report"
            log(f"[env_size] ERROR: failed reading report.json: {e}")
            report = None

        python_path = ""
        if isinstance(report, dict):
            python_path = str(report.get("python_path") or "")
        reported_python_path = python_path

        if not python_path:
            failure_category = "missing_report"
            log("[env_size] ERROR: report has no python_path")
        else:
            py = Path(python_path)
            if not py.exists():
                failure_category = "env_size_failed"
                log(f"[env_size] ERROR: python_path does not exist: {python_path}")
            elif not os.access(python_path, os.X_OK):
                failure_category = "env_size_failed"
                log(f"[env_size] ERROR: python_path is not executable: {python_path}")
            else:
                info_cmd = [
                    python_path,
                    "-c",
                    (
                        "import json, site, sys; "
                        "data={'sys_executable':sys.executable,'sys_prefix':sys.prefix,"
                        "'site_packages': (site.getsitepackages() if hasattr(site,'getsitepackages') else []),"
                        "'user_site': (site.getusersitepackages() if hasattr(site,'getusersitepackages') else '')}; "
                        "print(json.dumps(data))"
                    ),
                ]
                log(f"[env_size] info_cmd={' '.join(info_cmd)}")
                try:
                    proc = subprocess.run(info_cmd, capture_output=True, text=True, timeout=60)
                    log(proc.stdout.strip())
                    if proc.stderr.strip():
                        log(proc.stderr.strip())
                    if proc.returncode != 0:
                        raise RuntimeError(f"python_info_failed: returncode={proc.returncode}")
                    info = json.loads(proc.stdout.strip() or "{}")
                except Exception as e:
                    failure_category = "env_size_failed"
                    log(f"[env_size] ERROR: failed to query python env info: {e}")
                    info = {}

                env_prefix = Path(str(info.get("sys_prefix") or ""))
                site_packages: List[str] = []
                for p in info.get("site_packages") or []:
                    if isinstance(p, str) and p:
                        site_packages.append(p)
                user_site = info.get("user_site")
                if isinstance(user_site, str) and user_site:
                    site_packages.append(user_site)

                env_prefix_size = dir_size_bytes(env_prefix, warnings) if env_prefix else 0
                sp_entries: List[Dict[str, Any]] = []
                sp_total = 0
                for sp in site_packages:
                    sp_path = Path(sp)
                    size = dir_size_bytes(sp_path, warnings) if sp_path.exists() else 0
                    sp_entries.append({"path": sp, "size_bytes": size})
                    sp_total += int(size)

                observed = {
                    "env_prefix": str(env_prefix),
                    "env_prefix_size_MB": int(round(env_prefix_size / (1024 * 1024))),
                    "site_packages": sp_entries,
                    "site_packages_total_bytes": int(sp_total),
                }

                status = "success"
                exit_code = 0
                failure_category = "unknown"

        results = {
            "status": status,
            "skip_reason": "unknown",
            "exit_code": exit_code,
            "stage": "env_size",
            "task": "measure",
            "command": f"python {Path(__file__).name} --report-path {report_path}",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "reported_python_path": reported_python_path,
            "observed": observed,
            "meta": {
                "python": sys.executable,
                "git_commit": git_commit(root),
                "warnings": warnings,
            },
            "failure_category": failure_category,
            "error_excerpt": "",
        }

        write_json(results_path, results)

    # Fill error_excerpt after log is written.
    try:
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["error_excerpt"] = tail_lines(log_path)
        write_json(results_path, results)
    except Exception:
        pass

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

