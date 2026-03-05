#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


def load_report(report_path: Path) -> Tuple[Dict[str, Any] | None, str | None]:
    if not report_path.exists():
        return None, f"Report not found: {report_path}"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "Report JSON is not an object"
        return data, None
    except Exception as e:
        return None, f"Failed to parse report JSON: {e}"


def is_executable_file(path: Path) -> bool:
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode):
            return False
        return os.access(path, os.X_OK)
    except Exception:
        return False


def scandir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0

    def _walk(p: Path) -> None:
        nonlocal total
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            # Don't follow symlinks (avoid double counting / cycles).
                            try:
                                total += entry.stat(follow_symlinks=False).st_size
                            except Exception:
                                pass
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            _walk(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                total += entry.stat(follow_symlinks=False).st_size
                            except Exception as e:
                                warnings.append(f"stat failed: {entry.path}: {e}")
                    except PermissionError as e:
                        warnings.append(f"permission denied: {entry.path}: {e}")
        except PermissionError as e:
            warnings.append(f"permission denied: {p}: {e}")
        except FileNotFoundError:
            return
        except Exception as e:
            warnings.append(f"scandir failed: {p}: {e}")

    if path.exists():
        _walk(path)
    return total


def query_env_paths(python_path: str) -> Tuple[Dict[str, Any], str]:
    code = r"""
import json, site, sys
out = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_packages": [],
}
try:
  out["site_packages"] = list(site.getsitepackages())
except Exception:
  out["site_packages"] = []
try:
  out["user_site"] = site.getusersitepackages()
except Exception:
  out["user_site"] = ""
print(json.dumps(out))
"""
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    out = subprocess.check_output(
        [python_path, "-c", code],
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    return json.loads(out), out


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure environment size from report python_path.")
    parser.add_argument("--report-path", default="", help="Override report.json path.")
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = Path(
        args.report_path
        or os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH)
    )
    command = " ".join(
        shlex.quote(x)
        for x in [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    )

    timeout_sec = 120
    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    reported_python_path = ""
    observed: Dict[str, Any] = {}
    warnings: List[str] = []

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"[env_size] timestamp_utc={utc_timestamp()}\n")
        logf.write(f"[env_size] report_path={report_path}\n")
        logf.write(f"[env_size] command={command}\n")
        report, err = load_report(report_path)
        if err:
            logf.write(f"[env_size] ERROR: {err}\n")
        else:
            reported_python_path = str(report.get("python_path", "")).strip()
            logf.write(f"[env_size] reported_python_path={reported_python_path}\n")

        python_path_obj = Path(reported_python_path) if reported_python_path else None
        if err or not python_path_obj or not is_executable_file(python_path_obj):
            failure_category = "env_size_failed"
            status = "failure"
            exit_code = 1
        else:
            try:
                env_info, raw = query_env_paths(reported_python_path)
                logf.write(f"[env_size] env_info={raw.strip()}\n")
                env_prefix = Path(env_info.get("sys_prefix", ""))
                site_pkgs: List[str] = []
                sp = env_info.get("site_packages", [])
                if isinstance(sp, list):
                    site_pkgs.extend([str(x) for x in sp])
                user_site = env_info.get("user_site", "")
                if isinstance(user_site, str) and user_site:
                    site_pkgs.append(user_site)
                # de-dup, preserve order
                seen = set()
                site_pkgs = [p for p in site_pkgs if not (p in seen or seen.add(p))]

                env_prefix_size = scandir_size_bytes(env_prefix, warnings)
                site_entries = []
                site_total = 0
                for p in site_pkgs:
                    pp = Path(p)
                    sz = scandir_size_bytes(pp, warnings) if pp.exists() else 0
                    site_entries.append({"path": str(pp), "size_bytes": sz})
                    site_total += sz

                observed = {
                    "env_prefix": str(env_prefix),
                    "env_prefix_size_MB": int(round(env_prefix_size / (1024 * 1024))),
                    "site_packages": site_entries,
                    "site_packages_total_bytes": site_total,
                }
                status = "success"
                exit_code = 0
                failure_category = "unknown"
            except Exception:
                status = "failure"
                exit_code = 1
                failure_category = "env_size_failed"
                tb = traceback.format_exc()
                logf.write("[env_size] exception:\n")
                logf.write(tb)

        if warnings:
            logf.write("[env_size] warnings:\n")
            for w in warnings[:200]:
                logf.write(f"- {w}\n")

    results: Dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": command,
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
            "git_commit": git_commit(root),
            "env_vars": {
                k: os.environ.get(k, "")
                for k in [
                    "CUDA_VISIBLE_DEVICES",
                    "HF_HOME",
                    "TRANSFORMERS_CACHE",
                    "HF_DATASETS_CACHE",
                    "PIP_CACHE_DIR",
                    "XDG_CACHE_HOME",
                    "SENTENCE_TRANSFORMERS_HOME",
                    "TORCH_HOME",
                    "PYTHONDONTWRITEBYTECODE",
                    "SCIMLOPSBENCH_REPORT",
                    "SCIMLOPSBENCH_PYTHON",
                ]
            },
            "decision_reason": "Read python_path from report.json, query sys.prefix and site-packages via that interpreter, then recursively sum sizes (sys.prefix and site-packages only).",
            "timestamp_utc": utc_timestamp(),
            "warnings": warnings,
        },
        "failure_category": failure_category,
        "error_excerpt": tail_text(log_path),
    }

    results_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
