#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _resolve_report_path(cli_path: Optional[str]) -> Path:
    if cli_path:
        return Path(cli_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def _read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace")), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _is_executable(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def _dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    if not path.exists():
        warnings.append(f"missing_path: {path}")
        return 0
    for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        for name in files:
            fp = Path(root) / name
            try:
                total += fp.stat().st_size
            except PermissionError:
                warnings.append(f"permission_denied: {fp}")
            except FileNotFoundError:
                continue
            except OSError as e:
                warnings.append(f"oserror: {fp}: {e}")
    return total


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Measure environment size using python_path from the agent report.")
    parser.add_argument("--report-path", help="Override report path (else SCIMLOPSBENCH_REPORT or default).")
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)
    reported_python_path = ""
    report: Optional[Dict[str, Any]] = None
    report_err = None

    if report_path.exists():
        report, report_err = _read_json(report_path)
        if report and isinstance(report.get("python_path"), str):
            reported_python_path = report["python_path"]

    failure_category = "unknown"
    status = "success"
    exit_code = 0
    error_excerpt = ""

    if not report_path.exists() or report is None:
        status = "failure"
        exit_code = 1
        failure_category = "env_size_failed"
        error_excerpt = f"missing_or_invalid_report: {report_path} ({report_err or 'missing'})"
    elif not reported_python_path:
        status = "failure"
        exit_code = 1
        failure_category = "env_size_failed"
        error_excerpt = "python_path missing in report.json"
    elif not _is_executable(Path(reported_python_path)):
        status = "failure"
        exit_code = 1
        failure_category = "env_size_failed"
        error_excerpt = f"python_path not executable: {reported_python_path}"

    observed: Dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
    }

    warnings: List[str] = []
    if exit_code == 0:
        probe_code = (
            "import json, site, sys; "
            "print(json.dumps({'prefix': sys.prefix, "
            "'site': list(site.getsitepackages()) if hasattr(site,'getsitepackages') else [], "
            "'usersite': site.getusersitepackages() if hasattr(site,'getusersitepackages') else ''}))"
        )
        try:
            proc = subprocess.run(
                [reported_python_path, "-c", probe_code],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "").strip())
            probe = json.loads(proc.stdout.strip().splitlines()[-1])
            env_prefix = Path(probe.get("prefix", ""))
            observed["env_prefix"] = str(env_prefix)
            env_bytes = _dir_size_bytes(env_prefix, warnings)
            observed["env_prefix_size_MB"] = int(round(env_bytes / (1024 * 1024)))

            site_paths: List[str] = []
            for p in probe.get("site", []) or []:
                if isinstance(p, str) and p:
                    site_paths.append(p)
            usersite = probe.get("usersite", "")
            if isinstance(usersite, str) and usersite:
                site_paths.append(usersite)

            total_site = 0
            site_items = []
            for sp in site_paths:
                sp_path = Path(sp)
                size_b = _dir_size_bytes(sp_path, warnings)
                total_site += size_b
                site_items.append({"path": str(sp_path), "size_bytes": int(size_b)})
            observed["site_packages"] = site_items
            observed["site_packages_total_bytes"] = int(total_site)
        except Exception as e:
            status = "failure"
            exit_code = 1
            failure_category = "env_size_failed"
            error_excerpt = f"{type(e).__name__}: {e}"

    log_lines = [
        f"[env_size] timestamp_utc={_utc_now_iso()}",
        f"[env_size] report_path={report_path}",
        f"[env_size] reported_python_path={reported_python_path}",
        f"[env_size] status={status} exit_code={exit_code}",
    ]
    if warnings:
        log_lines.append("[env_size] warnings:")
        log_lines.extend(f"  - {w}" for w in warnings[:200])
    if error_excerpt:
        log_lines.append("[env_size] error_excerpt:")
        log_lines.append(error_excerpt)
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    results: Dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"python benchmark_scripts/measure_env_size.py --report-path {report_path}",
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "git_commit": None,
            "timestamp_utc": _utc_now_iso(),
            "warnings": warnings,
        },
        "failure_category": failure_category if exit_code != 0 else "unknown",
        "error_excerpt": error_excerpt,
    }

    try:
        results["meta"]["git_commit"] = (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True).strip()
        )
    except Exception:
        results["meta"]["git_commit"] = None

    _write_json(results_path, results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

