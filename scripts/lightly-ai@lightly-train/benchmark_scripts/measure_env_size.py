#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tail_text(path: Path, max_lines: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            dq: deque[str] = deque(f, maxlen=max_lines)
        return "".join(dq).strip()
    except Exception:
        return ""


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def is_executable(path: Path) -> bool:
    try:
        st = path.stat()
        return bool(st.st_mode & stat.S_IXUSR) and path.is_file()
    except Exception:
        return False


def dir_size_bytes(path: Path, warnings: list[str]) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
            # Skip typical caches inside env to avoid runaway traversal if present.
            dirs[:] = [d for d in dirs if d not in {".cache", "__pycache__"}]
            for name in files:
                fp = Path(root) / name
                try:
                    if fp.is_symlink():
                        continue
                    total += fp.stat().st_size
                except PermissionError as exc:
                    warnings.append(f"permission_error: {fp}: {exc}")
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    warnings.append(f"os_error: {fp}: {exc}")
    except PermissionError as exc:
        warnings.append(f"permission_error_walk: {path}: {exc}")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"walk_error: {path}: {exc}")
    return total


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    stage_dir = repo_root / "build_output" / "env_size"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    started_utc = utc_now()
    report_path = resolve_report_path(args.report_path)

    status = "failure"
    failure_category = "env_size_failed"
    error_excerpt = ""
    observed: dict[str, Any] = {}

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[env_size] started_utc={started_utc}\n")
        log.write(f"[env_size] report_path={report_path}\n")

        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            error_excerpt = f"missing report: {report_path}"
            report = None
        except Exception as exc:  # noqa: BLE001
            error_excerpt = f"invalid report json: {report_path}: {exc}"
            report = None

        python_path = None
        if isinstance(report, dict):
            python_path = report.get("python_path")

        if not isinstance(python_path, str) or not python_path.strip():
            error_excerpt = error_excerpt or "report missing python_path"
            python_path = None

        if python_path is not None and not is_executable(Path(python_path)):
            error_excerpt = f"python_path not executable: {python_path}"
            python_path = None

        warnings: list[str] = []
        if python_path:
            snippet = (
                "import json, site, sys; "
                "print(json.dumps({'sys_prefix': sys.prefix, "
                "'site_packages': site.getsitepackages() if hasattr(site,'getsitepackages') else [], "
                "'user_site': site.getusersitepackages() if hasattr(site,'getusersitepackages') else ''}))"
            )
            try:
                proc = subprocess.run(
                    [python_path, "-c", snippet],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                log.write(proc.stdout)
                if proc.stderr:
                    log.write("\n[env_size] stderr:\n")
                    log.write(proc.stderr)
                if proc.returncode != 0:
                    error_excerpt = f"python probe failed: rc={proc.returncode}"
                    python_path = None
                else:
                    probe = json.loads(proc.stdout.strip() or "{}")
                    env_prefix = Path(str(probe.get("sys_prefix", "")))
                    site_paths = [Path(p) for p in (probe.get("site_packages") or []) if isinstance(p, str)]
                    user_site = probe.get("user_site")
                    if isinstance(user_site, str) and user_site:
                        site_paths.append(Path(user_site))

                    env_prefix_size = dir_size_bytes(env_prefix, warnings) if env_prefix.exists() else 0
                    site_sizes: list[dict[str, Any]] = []
                    total_site = 0
                    for sp in site_paths:
                        if sp.exists():
                            sz = dir_size_bytes(sp, warnings)
                            site_sizes.append({"path": str(sp), "size_bytes": sz})
                            total_site += sz
                        else:
                            site_sizes.append({"path": str(sp), "size_bytes": 0})

                    observed = {
                        "env_prefix": str(env_prefix),
                        "env_prefix_size_MB": int(env_prefix_size / (1024 * 1024)),
                        "site_packages": site_sizes,
                        "site_packages_total_bytes": total_site,
                        "warnings": warnings,
                    }
                    status = "success"
                    failure_category = ""
            except subprocess.TimeoutExpired:
                error_excerpt = "python probe timed out"
            except Exception as exc:  # noqa: BLE001
                error_excerpt = str(exc)

        finished_utc = utc_now()
        log.write(f"\n[env_size] finished_utc={finished_utc}\n")
        if warnings:
            log.write("[env_size] warnings:\n")
            for w in warnings:
                log.write(f"  - {w}\n")

    exit_code = 0 if status == "success" else 1
    if not error_excerpt and status == "failure":
        error_excerpt = tail_text(log_path, 220)

    payload: dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": "python measure_env_size.py",
        "reported_python_path": str(python_path) if python_path else None,
        "observed": observed,
        "meta": {
            "report_path": str(report_path),
            "started_utc": started_utc,
            "finished_utc": utc_now(),
        },
        "failure_category": ("env_size_failed" if status == "failure" else ""),
        "error_excerpt": error_excerpt[-4000:] if error_excerpt else "",
    }
    write_json(results_path, payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
