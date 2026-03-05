#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


REPORT_PATH_DEFAULT = "/opt/scimlopsbench/report.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _git_commit(repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
    except Exception:
        return ""


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(REPORT_PATH_DEFAULT)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _dir_size_bytes(path: Path) -> Tuple[int, List[str]]:
    total = 0
    warnings: List[str] = []
    if not path.exists():
        return 0, [f"path does not exist: {path}"]
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except PermissionError as e:
            warnings.append(f"permission_error: {p}: {e}")
        except FileNotFoundError:
            continue
        except Exception as e:
            warnings.append(f"error: {p}: {e}")
    return total, warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--out-dir", default="build_output/env_size")
    args = parser.parse_args()

    repo_root = _repo_root()
    out_dir = (repo_root / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)
    reported = _read_json(report_path)
    python_path = str(reported.get("python_path", "") or "")

    def write_results(status: str, exit_code: int, failure_category: str, observed: Dict[str, Any], error_excerpt: str) -> None:
        payload: Dict[str, Any] = {
            "status": status,
            "exit_code": exit_code,
            "stage": "env_size",
            "task": "measure",
            "command": " ".join([sys.executable, "benchmark_scripts/measure_env_size.py"] + sys.argv[1:]),
            "reported_python_path": python_path,
            "observed": observed,
            "meta": {
                "git_commit": _git_commit(repo_root),
                "timestamp_utc": _utc_now_iso(),
                "report_path": str(report_path),
            },
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[env_size] timestamp_utc={_utc_now_iso()}\n")
        log.write(f"[env_size] report_path={report_path}\n")
        log.write(f"[env_size] reported_python_path={python_path}\n")

    if not python_path or not Path(python_path).exists():
        excerpt = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:])
        write_results(
            status="failure",
            exit_code=1,
            failure_category="env_size_failed",
            observed={},
            error_excerpt=excerpt or "report missing python_path or python_path does not exist",
        )
        return 1

    # Query env prefix + site-packages via the reported python.
    probe_code = r"""
import json, site, sys
payload = {
  "sys_prefix": sys.prefix,
  "site_packages": list(dict.fromkeys([p for p in (site.getsitepackages() if hasattr(site, "getsitepackages") else []) if isinstance(p, str)])),
  "user_site": site.getusersitepackages() if hasattr(site, "getusersitepackages") else "",
}
print(json.dumps(payload))
"""
    try:
        out = subprocess.check_output([python_path, "-c", probe_code], text=True, stderr=subprocess.STDOUT, timeout=30).strip()
        probe = json.loads(out)
    except Exception as e:
        excerpt = f"probe failed: {e}"
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[env_size] ERROR: {excerpt}\n")
        excerpt2 = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:])
        write_results("failure", 1, "env_size_failed", {}, excerpt2)
        return 1

    env_prefix = Path(str(probe.get("sys_prefix", "")))
    site_packages: List[str] = []
    for p in probe.get("site_packages", []):
        if isinstance(p, str) and p:
            site_packages.append(p)
    user_site = probe.get("user_site", "")
    if isinstance(user_site, str) and user_site:
        site_packages.append(user_site)

    env_size, env_warnings = _dir_size_bytes(env_prefix)
    sp_entries: List[Dict[str, Any]] = []
    sp_total = 0
    warnings: List[str] = list(env_warnings)
    for sp in site_packages:
        sp_path = Path(sp)
        size, ws = _dir_size_bytes(sp_path)
        sp_entries.append({"path": str(sp_path), "size_bytes": size})
        sp_total += size
        warnings.extend(ws)

    observed = {
        "env_prefix": str(env_prefix),
        "env_prefix_size_MB": int(round(env_size / (1024 * 1024))),
        "site_packages": sp_entries,
        "site_packages_total_bytes": sp_total,
        "warnings": warnings,
    }
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"[env_size] env_prefix={env_prefix} size_bytes={env_size}\n")
        log.write(f"[env_size] site_packages_total_bytes={sp_total}\n")
        if warnings:
            log.write(f"[env_size] warnings_count={len(warnings)}\n")

    excerpt = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:])
    write_results("success", 0, "unknown", observed, excerpt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

