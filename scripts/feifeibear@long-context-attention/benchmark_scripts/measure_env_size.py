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
from typing import Any, Dict, List, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root),
                text=True,
                stderr=subprocess.DEVNULL,
            )
            .strip()
            .strip()
        )
    except Exception:
        return ""


def resolve_report_path(cli_path: str) -> Path:
    if cli_path:
        return Path(cli_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return DEFAULT_REPORT_PATH


def safe_walk_size(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        if not path.exists():
            return 0
    except Exception as e:
        warnings.append(f"stat_failed:{path}:{e}")
        return 0

    for root, dirs, files in os.walk(path, onerror=None, followlinks=False):
        for name in files:
            fp = Path(root) / name
            try:
                total += fp.stat().st_size
            except PermissionError:
                warnings.append(f"permission_denied:{fp}")
            except FileNotFoundError:
                continue
            except Exception as e:
                warnings.append(f"stat_failed:{fp}:{e}")
    return total


def probe_env_paths(python_path: str) -> Dict[str, Any]:
    code = r"""
import json, site, sys
out = {"sys_executable": sys.executable, "sys_prefix": sys.prefix, "site_packages": [], "user_site_packages": ""}
try:
    out["site_packages"] = list(site.getsitepackages())
except Exception:
    out["site_packages"] = []
try:
    out["user_site_packages"] = site.getusersitepackages()
except Exception:
    out["user_site_packages"] = ""
print(json.dumps(out))
"""
    p = subprocess.run(
        [python_path, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip())
    return json.loads(p.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    cmd_str = " ".join(shlex.quote(a) for a in sys.argv)
    report_path = resolve_report_path(args.report_path)

    env_vars = {
        "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
        "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
    }

    base_payload: Dict[str, Any] = {
        "status": "failure",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": cmd_str,
        "reported_python_path": "",
        "observed": {},
        "meta": {
            "python": sys.executable,
            "git_commit": git_commit(root),
            "env_vars": env_vars,
            "decision_reason": "Measure size of the benchmark python environment (sys.prefix and site-packages) using python_path from the agent report.",
            "timestamp_utc": utc(),
            "report_path": str(report_path),
            "warnings": [],
        },
        "failure_category": "env_size_failed",
        "error_excerpt": "",
    }

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        python_path = str(report.get("python_path") or "")
        base_payload["reported_python_path"] = python_path
        if not python_path:
            raise RuntimeError("report.json missing python_path")
        if not (Path(python_path).exists() and os.access(python_path, os.X_OK)):
            raise RuntimeError(f"python_path not executable: {python_path}")
    except Exception as e:
        base_payload["error_excerpt"] = str(e)
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"[env_size] started_utc={utc()}\n")
            log.write(f"[env_size] error={e}\n")
            log.write(f"[env_size] ended_utc={utc()}\n")
        write_json(results_path, base_payload)
        return 1

    warnings: List[str] = []
    try:
        probed = probe_env_paths(python_path)
        env_prefix = Path(probed.get("sys_prefix") or "").resolve()
        site_packages: List[str] = list(probed.get("site_packages") or [])
        user_site = probed.get("user_site_packages") or ""
        if user_site:
            site_packages.append(user_site)

        env_prefix_size = safe_walk_size(env_prefix, warnings)
        site_entries: List[Dict[str, Any]] = []
        site_total = 0
        for sp in site_packages:
            sp_path = Path(sp)
            sp_size = safe_walk_size(sp_path, warnings)
            site_entries.append({"path": str(sp_path), "size_bytes": sp_size})
            site_total += sp_size

        observed = {
            "env_prefix": str(env_prefix),
            "env_prefix_size_MB": int(round(env_prefix_size / (1024 * 1024))),
            "site_packages": site_entries,
            "site_packages_total_bytes": int(site_total),
        }
        base_payload["observed"] = observed
        base_payload["status"] = "success"
        base_payload["exit_code"] = 0
        base_payload["failure_category"] = "unknown"
        base_payload["error_excerpt"] = ""
        base_payload["meta"]["warnings"] = warnings
    except Exception as e:
        base_payload["error_excerpt"] = str(e)
        base_payload["meta"]["warnings"] = warnings

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[env_size] started_utc={utc()}\n")
        log.write(f"[env_size] command={cmd_str}\n")
        log.write(f"[env_size] report_path={report_path}\n")
        log.write(f"[env_size] python_path={python_path}\n")
        for w in warnings:
            log.write(f"[env_size] warning={w}\n")
        log.write(f"[env_size] status={base_payload['status']}\n")
        log.write(f"[env_size] ended_utc={utc()}\n")

    # Add global-required fields for consistency with other stages.
    base_payload.setdefault(
        "assets",
        {
            "dataset": {
                "path": str((root / "benchmark_assets" / "dataset").resolve()),
                "source": "not_applicable",
                "version": "unknown",
                "sha256": "",
            },
            "model": {
                "path": str((root / "benchmark_assets" / "model").resolve()),
                "source": "not_applicable",
                "version": "unknown",
                "sha256": "",
            },
        },
    )
    base_payload.setdefault("skip_reason", "unknown")
    base_payload.setdefault("framework", "unknown")

    write_json(results_path, base_payload)
    try:
        return int(base_payload.get("exit_code", 1))
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
