#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json: {path}: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"failed reading json: {path}: {e}"


def tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            if p.is_symlink():
                continue
            if p.is_file():
                try:
                    total += p.stat().st_size
                except PermissionError as e:
                    warnings.append(f"permission_error: {p}: {e}")
                except FileNotFoundError:
                    pass
                continue
            if p.is_dir():
                try:
                    with os.scandir(p) as it:
                        for entry in it:
                            stack.append(Path(entry.path))
                except PermissionError as e:
                    warnings.append(f"permission_error: {p}: {e}")
                except FileNotFoundError:
                    pass
        except Exception as e:  # noqa: BLE001
            warnings.append(f"walk_error: {p}: {e}")
    return total


def resolve_report_path(cli_path: Optional[str]) -> str:
    if cli_path:
        return cli_path
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return env_path
    return DEFAULT_REPORT_PATH


def load_assets(root: Path) -> Dict[str, Dict[str, str]]:
    manifest = root / "benchmark_assets" / "manifest.json"
    data, _ = safe_read_json(manifest)
    if not isinstance(data, dict):
        return {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        }
    ds = data.get("dataset") if isinstance(data.get("dataset"), dict) else {}
    md = data.get("model") if isinstance(data.get("model"), dict) else {}
    return {
        "dataset": {
            "path": str(ds.get("path", "")),
            "source": str(ds.get("source", "")),
            "version": str(ds.get("version", "")),
            "sha256": str(ds.get("sha256", "")),
        },
        "model": {
            "path": str(md.get("path", "")),
            "source": str(md.get("source", "")),
            "version": str(md.get("version", "")),
            "sha256": str(md.get("sha256", "")),
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Measure environment size for the agent-reported python.")
    parser.add_argument("--report-path", default="")
    args = parser.parse_args(argv)

    root = repo_root()
    out_dir = root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    log_path.write_text("", encoding="utf-8")

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")

    report_path = resolve_report_path(args.report_path or None)
    log(f"[env_size] timestamp_utc={utc_ts()}")
    log(f"[env_size] report_path={report_path}")

    assets = load_assets(root)

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    error_excerpt = ""

    payload: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": f"{sys.executable} benchmark_scripts/measure_env_size.py --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {"dataset": assets["dataset"], "model": assets["model"]},
        "reported_python_path": "",
        "observed": {
            "env_prefix": "",
            "env_prefix_size_MB": 0,
            "site_packages": [],
            "site_packages_total_bytes": 0,
        },
        "meta": {
            "python": sys.executable,
            "python_version": platform.python_version(),
            "timestamp_utc": utc_ts(),
            "warnings": [],
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            },
        },
        "failure_category": failure_category,
        "error_excerpt": "",
    }

    report, err = safe_read_json(Path(report_path))
    if report is None or not isinstance(report, dict):
        log(f"[env_size] report error: {err}")
        payload["failure_category"] = "env_size_failed"
        payload["error_excerpt"] = err or "missing/invalid report"
        write_json(results_path, payload)
        return 1

    python_path = str(report.get("python_path", "")).strip()
    payload["reported_python_path"] = python_path
    if not python_path:
        log("[env_size] python_path missing in report.json")
        payload["failure_category"] = "env_size_failed"
        payload["error_excerpt"] = "python_path missing in report.json"
        write_json(results_path, payload)
        return 1

    py = Path(python_path)
    if not py.exists() or not os.access(str(py), os.X_OK):
        log(f"[env_size] python_path not executable: {python_path}")
        payload["failure_category"] = "env_size_failed"
        payload["error_excerpt"] = f"python_path not executable: {python_path}"
        write_json(results_path, payload)
        return 1

    # Ask the reported python for sys.prefix and site-packages.
    probe_code = r"""
import json, site, sys
out = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_getsitepackages": [],
  "site_getusersitepackages": "",
}
try:
  out["site_getsitepackages"] = list(site.getsitepackages())
except Exception:
  out["site_getsitepackages"] = []
try:
  out["site_getusersitepackages"] = site.getusersitepackages()
except Exception:
  out["site_getusersitepackages"] = ""
print(json.dumps(out))
"""
    try:
        res = subprocess.run(
            [python_path, "-c", probe_code],
            check=True,
            text=True,
            capture_output=True,
            timeout=60,
        )
        probe = json.loads(res.stdout.strip() or "{}")
    except Exception as e:  # noqa: BLE001
        log(f"[env_size] failed to probe environment via reported python: {e}")
        payload["failure_category"] = "env_size_failed"
        payload["error_excerpt"] = f"failed to probe environment via reported python: {e}"
        write_json(results_path, payload)
        return 1

    env_prefix = str(probe.get("sys_prefix", ""))
    if not env_prefix:
        log("[env_size] sys.prefix empty from probe")
        payload["failure_category"] = "env_size_failed"
        payload["error_excerpt"] = "sys.prefix empty from probe"
        write_json(results_path, payload)
        return 1

    warnings: List[str] = []
    env_prefix_path = Path(env_prefix)
    log(f"[env_size] env_prefix={env_prefix_path}")

    env_bytes = dir_size_bytes(env_prefix_path, warnings)
    site_paths: List[str] = []
    for p in probe.get("site_getsitepackages", []) or []:
        if isinstance(p, str) and p:
            site_paths.append(p)
    usp = probe.get("site_getusersitepackages", "")
    if isinstance(usp, str) and usp:
        site_paths.append(usp)

    site_entries = []
    site_total = 0
    for sp in site_paths:
        sp_path = Path(sp)
        if not sp_path.exists():
            warnings.append(f"site_packages_missing: {sp}")
            continue
        size = dir_size_bytes(sp_path, warnings)
        site_entries.append({"path": str(sp_path), "size_bytes": size})
        site_total += size

    payload.update(
        {
            "status": "success",
            "exit_code": 0,
            "observed": {
                "env_prefix": str(env_prefix_path),
                "env_prefix_size_MB": int(env_bytes / (1024 * 1024)),
                "site_packages": site_entries,
                "site_packages_total_bytes": site_total,
            },
            "meta": {
                **payload["meta"],
                "reported_python_executable": str(probe.get("sys_executable", "")),
                "warnings": warnings,
            },
            "failure_category": "unknown",
            "error_excerpt": "",
        }
    )

    write_json(results_path, payload)
    log("[env_size] success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
