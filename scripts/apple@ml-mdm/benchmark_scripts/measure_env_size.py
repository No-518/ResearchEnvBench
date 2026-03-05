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


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def read_git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).strip()
        return out
    except Exception:
        return ""


def empty_assets() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, ""
    except FileNotFoundError:
        return None, "missing_report"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "unknown"


def resolve_report_path(cli_report_path: str) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(DEFAULT_REPORT_PATH)


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
                        except Exception as e:
                            warnings.append(f"stat_failed:{entry.path}:{e}")
                    elif entry.is_dir(follow_symlinks=False):
                        total += dir_size_bytes(Path(entry.path), warnings)
                except PermissionError as e:
                    warnings.append(f"permission_denied:{entry.path}:{e}")
                except FileNotFoundError:
                    continue
                except Exception as e:
                    warnings.append(f"scan_failed:{entry.path}:{e}")
    except PermissionError as e:
        warnings.append(f"permission_denied:{path}:{e}")
    except FileNotFoundError:
        warnings.append(f"not_found:{path}")
    except Exception as e:
        warnings.append(f"scan_failed:{path}:{e}")
    return total


SITE_INFO_CODE = r"""
import json, site, sys
out = {
  "sys_prefix": sys.prefix,
  "site_packages": [],
  "user_site": "",
}
try:
  out["site_packages"] = list(site.getsitepackages())
except Exception:
  out["site_packages"] = []
try:
  out["user_site"] = site.getusersitepackages() or ""
except Exception:
  out["user_site"] = ""
print(json.dumps(out))
"""


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Measure environment size based on report.json python_path.")
    ap.add_argument("--report-path", default="", help="Override report.json path (highest priority).")
    args = ap.parse_args(argv)

    out_dir = REPO_ROOT / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    report_path = resolve_report_path(args.report_path)
    report, report_err = load_json(report_path)

    base: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": f"python {Path(__file__).name} --report-path {str(report_path)}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": empty_assets(),
        "reported_python_path": "",
        "observed": {},
        "meta": {
            "python": f"{sys.executable} ({platform.python_version()})",
            "git_commit": read_git_commit(),
            "timestamp_utc": now_utc_iso(),
            "report_path": str(report_path),
        },
        "failure_category": "env_size_failed",
        "error_excerpt": "",
    }

    if report is None:
        log_path.write_text(f"failed_to_load_report: {report_err}\n", encoding="utf-8")
        base["error_excerpt"] = tail_lines(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    python_path = str(report.get("python_path") or "").strip()
    base["reported_python_path"] = python_path
    if not python_path:
        log_path.write_text("report.json missing python_path\n", encoding="utf-8")
        base["error_excerpt"] = tail_lines(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    if not (Path(python_path).is_file() and os.access(python_path, os.X_OK)):
        log_path.write_text(f"python_path not executable: {python_path}\n", encoding="utf-8")
        base["error_excerpt"] = tail_lines(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    try:
        proc = subprocess.run(
            [python_path, "-c", SITE_INFO_CODE],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        log_path.write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8", errors="replace")
    except Exception as e:
        log_path.write_text(f"failed_to_query_site_info: {e}\n", encoding="utf-8")
        base["error_excerpt"] = tail_lines(log_path)
        results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    try:
        site_info = json.loads(proc.stdout.strip() or "{}")
    except Exception:
        site_info = {}

    env_prefix = Path(str(site_info.get("sys_prefix") or "")).resolve() if site_info else Path()
    site_packages: List[str] = []
    if isinstance(site_info, dict):
        sp = site_info.get("site_packages")
        if isinstance(sp, list):
            site_packages = [str(x) for x in sp if x]
        user_site = str(site_info.get("user_site") or "")
        if user_site:
            site_packages.append(user_site)

    warnings: List[str] = []
    observed: Dict[str, Any] = {
        "env_prefix": str(env_prefix) if str(env_prefix) else "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
        "warnings": warnings,
    }

    if env_prefix and env_prefix.exists():
        env_bytes = dir_size_bytes(env_prefix, warnings)
        observed["env_prefix_size_MB"] = int(env_bytes / (1024 * 1024))
    else:
        warnings.append(f"env_prefix_not_found:{env_prefix}")

    sp_total = 0
    for p in site_packages:
        pp = Path(p)
        if not pp.exists():
            warnings.append(f"site_packages_not_found:{p}")
            continue
        size = dir_size_bytes(pp, warnings)
        observed["site_packages"].append({"path": str(pp), "size_bytes": size})
        sp_total += size
    observed["site_packages_total_bytes"] = sp_total

    base["observed"] = observed
    base["status"] = "success"
    base["exit_code"] = 0
    base["failure_category"] = "unknown"
    base["error_excerpt"] = tail_lines(log_path)
    results_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

