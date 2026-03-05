#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        txt = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"missing: {path}"
    except Exception as e:
        return None, f"read_failed: {path}: {e}"
    try:
        obj = json.loads(txt)
    except Exception as e:
        return None, f"invalid_json: {path}: {e}"
    if not isinstance(obj, dict):
        return None, f"invalid_json: {path}: expected object"
    return obj, None


def _report_path(cli: Optional[str]) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def _is_executable(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except Exception:
        return False


def _walk_size_bytes(root: Path, warnings: List[str]) -> int:
    total = 0

    def rec(p: Path) -> None:
        nonlocal total
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            try:
                                total += entry.stat(follow_symlinks=False).st_size
                            except Exception as e:
                                warnings.append(f"stat_failed: {entry.path}: {e}")
                        elif entry.is_dir(follow_symlinks=False):
                            rec(Path(entry.path))
                    except PermissionError as e:
                        warnings.append(f"permission_denied: {entry.path}: {e}")
                    except FileNotFoundError:
                        continue
        except PermissionError as e:
            warnings.append(f"permission_denied: {p}: {e}")
        except FileNotFoundError:
            return
        except Exception as e:
            warnings.append(f"walk_failed: {p}: {e}")

    rec(root)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None, help="Overrides report path (highest priority)")
    args = ap.parse_args()

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(line: str) -> None:
        msg = f"[{_utc_now_iso()}] {line}"
        print(msg)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    log_path.write_text("", encoding="utf-8")

    report_path = _report_path(args.report_path)
    report_obj, report_err = _read_json(report_path)

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    observed: Dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
    }
    warnings: List[str] = []
    reported_python_path = ""

    if report_obj is None:
        log(f"report_error={report_err}")
    else:
        reported_python_path = str(report_obj.get("python_path", "") or "")
        if not reported_python_path:
            log("report missing python_path")
            warnings.append("report.python_path missing")
        elif not _is_executable(reported_python_path):
            log(f"python_path not executable: {reported_python_path}")
            warnings.append("report.python_path not executable")
        else:
            # Query env prefix & site-packages from the reported python.
            try:
                probe = (
                    "import json,sys,site; "
                    "obj={'sys_prefix':sys.prefix,"
                    "'site_packages':site.getsitepackages() if hasattr(site,'getsitepackages') else [],"
                    "'user_site':site.getusersitepackages() if hasattr(site,'getusersitepackages') else ''}; "
                    "print(json.dumps(obj))"
                )
                out = subprocess.check_output([reported_python_path, "-c", probe], stderr=subprocess.STDOUT, timeout=60)
                info = json.loads(out.decode("utf-8", errors="replace"))
            except Exception as e:
                log(f"probe_failed: {e}")
                warnings.append(f"probe_failed: {e}")
                info = {}

            env_prefix = Path(str(info.get("sys_prefix", "") or ""))
            site_pkgs = [Path(p) for p in (info.get("site_packages") or []) if isinstance(p, str)]
            user_site = info.get("user_site")
            if isinstance(user_site, str) and user_site:
                site_pkgs.append(Path(user_site))

            if env_prefix and env_prefix.exists():
                size_bytes = _walk_size_bytes(env_prefix, warnings)
                observed["env_prefix"] = str(env_prefix)
                observed["env_prefix_size_MB"] = int(size_bytes / (1024 * 1024))
            else:
                warnings.append(f"env_prefix missing: {env_prefix}")

            sp_entries: List[Dict[str, Any]] = []
            sp_total = 0
            for sp in site_pkgs:
                if not sp or not sp.exists():
                    warnings.append(f"site_packages missing: {sp}")
                    continue
                sz = _walk_size_bytes(sp, warnings)
                sp_entries.append({"path": str(sp), "size_bytes": sz})
                sp_total += sz
            observed["site_packages"] = sp_entries
            observed["site_packages_total_bytes"] = sp_total

            status = "success"
            exit_code = 0
            failure_category = "not_applicable"

    error_excerpt = ""
    if exit_code != 0:
        error_excerpt = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:])

    results = {
        "status": status,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"{sys.executable} benchmark_scripts/measure_env_size.py --report-path {report_path}",
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "python": f"{sys.executable} ({platform.python_version()})",
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_now_iso(),
            "env_vars": {k: os.environ.get(k, "") for k in ["SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON"] if k in os.environ},
            "warnings": warnings,
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

