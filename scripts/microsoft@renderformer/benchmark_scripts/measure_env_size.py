#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git_commit(repo_root: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:
        return ""


def _tail(path: Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-n:])


def _safe_json_load(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except Exception as e:
        return None, f"invalid json: {path}: {e}"


def _is_executable(path: str) -> bool:
    try:
        p = Path(path)
        if not p.is_file():
            return False
        mode = p.stat().st_mode
        return bool(mode & stat.S_IXUSR) and os.access(str(p), os.X_OK)
    except Exception:
        return False


def _get_env_paths(python_path: str, timeout_sec: int = 30) -> Tuple[Optional[Dict[str, Any]], str]:
    code = r"""
import json, site, sys
site_packages = []
try:
  site_packages = site.getsitepackages() or []
except Exception:
  site_packages = []
user_site = ""
try:
  user_site = site.getusersitepackages() or ""
except Exception:
  user_site = ""
print(json.dumps({"sys_prefix": sys.prefix, "site_packages": site_packages, "user_site": user_site}))
"""
    try:
        res = subprocess.run(
            [python_path, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        if res.returncode != 0:
            return None, f"python probe failed (rc={res.returncode}): {res.stderr.strip()}"
        return json.loads(res.stdout.strip() or "{}"), ""
    except Exception as e:
        return None, f"python probe exception: {e}"


def _dir_size_bytes(root: Path, warnings: List[str]) -> int:
    total = 0
    stack = [root]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            st = entry.stat(follow_symlinks=False)
                            total += int(st.st_size)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            total += int(st.st_size)
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            st = entry.stat(follow_symlinks=False)
                            total += int(st.st_size)
                    except PermissionError as e:
                        warnings.append(f"permission_error: {entry.path}: {e}")
                    except FileNotFoundError:
                        continue
                    except Exception as e:
                        warnings.append(f"stat_error: {entry.path}: {e}")
        except PermissionError as e:
            warnings.append(f"permission_error: {p}: {e}")
        except FileNotFoundError:
            continue
        except Exception as e:
            warnings.append(f"scan_error: {p}: {e}")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure environment size based on report.json python_path.")
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = args.report_path or os.environ.get("SCIMLOPSBENCH_REPORT") or DEFAULT_REPORT_PATH
    logs: list[str] = []
    logs.append(f"[env_size] timestamp_utc={_utc_timestamp()}")
    logs.append(f"[env_size] report_path={report_path}")

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    reported_python_path = ""
    observed: Dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
    }
    warnings: List[str] = []

    report_data, report_err = _safe_json_load(Path(report_path))
    if report_data is None:
        logs.append(f"[env_size] report_error={report_err}")
    else:
        python_path = report_data.get("python_path")
        if not isinstance(python_path, str) or not python_path.strip():
            logs.append("[env_size] report missing python_path")
        else:
            reported_python_path = python_path
            logs.append(f"[env_size] reported_python_path={reported_python_path}")
            if not _is_executable(reported_python_path):
                logs.append("[env_size] python_path is not executable")
            else:
                env_paths, err = _get_env_paths(reported_python_path)
                if env_paths is None:
                    logs.append(f"[env_size] probe_error={err}")
                else:
                    env_prefix = str(env_paths.get("sys_prefix") or "")
                    observed["env_prefix"] = env_prefix
                    site_pkgs = list(env_paths.get("site_packages") or [])
                    user_site = str(env_paths.get("user_site") or "")
                    if user_site:
                        site_pkgs.append(user_site)

                    logs.append(f"[env_size] env_prefix={env_prefix}")
                    logs.append(f"[env_size] site_packages={site_pkgs}")

                    if env_prefix and Path(env_prefix).exists():
                        env_bytes = _dir_size_bytes(Path(env_prefix), warnings)
                        observed["env_prefix_size_MB"] = int(env_bytes / (1024 * 1024))
                    else:
                        warnings.append("env_prefix_missing_or_not_found")

                    sp_entries = []
                    sp_total = 0
                    for sp in site_pkgs:
                        sp_path = Path(sp)
                        if not sp_path.exists():
                            sp_entries.append({"path": str(sp_path), "size_bytes": 0, "missing": True})
                            continue
                        size_b = _dir_size_bytes(sp_path, warnings)
                        sp_total += size_b
                        sp_entries.append({"path": str(sp_path), "size_bytes": size_b})
                    observed["site_packages"] = sp_entries
                    observed["site_packages_total_bytes"] = sp_total

                    status = "success"
                    exit_code = 0
                    failure_category = "unknown"

    log_text = "\n".join(logs + ([f"[env_size] warnings={warnings}"] if warnings else [])) + "\n"
    log_path.write_text(log_text, encoding="utf-8")

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"{sys.executable} benchmark_scripts/measure_env_size.py --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_timestamp(),
            "warnings": warnings,
            "python": sys.executable,
            "env_vars": {k: os.environ.get(k, "") for k in ["SCIMLOPSBENCH_REPORT"] if os.environ.get(k)},
            "decision_reason": "Measure disk usage of sys.prefix and site-packages paths for the reported python environment.",
        },
        "failure_category": failure_category,
        "error_excerpt": _tail(log_path, n=220),
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
