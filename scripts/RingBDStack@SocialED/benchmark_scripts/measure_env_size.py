#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return DEFAULT_REPORT_PATH


def load_report(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not report_path.exists():
        return None, "missing_report"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def git_commit(root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT, text=True)
        return out.strip()
    except Exception:
        return ""


def dir_size_bytes(path: Path) -> Tuple[int, List[str]]:
    total = 0
    warnings: List[str] = []
    if not path.exists():
        return 0, [f"missing:{path}"]
    for root, dirs, files in os.walk(path, topdown=True):
        # Avoid following symlinks to keep measurement stable.
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for f in files:
            fp = Path(root) / f
            try:
                if fp.is_symlink():
                    continue
                total += fp.stat().st_size
            except PermissionError:
                warnings.append(f"permission_denied:{fp}")
            except FileNotFoundError:
                warnings.append(f"vanished:{fp}")
            except Exception as e:
                warnings.append(f"error:{fp}:{e}")
    return total, warnings


def probe_env_paths(python_path: str) -> Tuple[int, str, str]:
    code = r"""
import json
import site
import sys

payload = {
  "python_executable": sys.executable,
  "python_version": sys.version.split()[0],
  "env_prefix": sys.prefix,
  "site_packages": [],
}

try:
  payload["site_packages"] = site.getsitepackages()
except Exception as e:
  payload["site_packages_error"] = str(e)

try:
  payload["user_site_packages"] = site.getusersitepackages()
except Exception as e:
  payload["user_site_packages_error"] = str(e)

print(json.dumps(payload))
"""
    try:
        completed = subprocess.run(
            [python_path, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1"),
        )
        return int(completed.returncode), completed.stdout, completed.stderr
    except Exception as e:
        return 1, "", str(e)


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure environment size for reported python_path.")
    ap.add_argument("--report-path", default=None, help="Override report.json path.")
    args = ap.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "env_size"
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report, report_err = load_report(report_path)

    results: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": f"{sys.executable} {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "reported_python_path": "",
        "observed": {
            "env_prefix": "",
            "env_prefix_size_MB": 0,
            "site_packages": [],
            "site_packages_total_bytes": 0,
        },
        "meta": {
            "timestamp_utc": utc_timestamp(),
            "python": sys.executable,
            "git_commit": git_commit(root),
            "report_path": str(report_path),
        },
        "failure_category": "env_size_failed",
        "error_excerpt": "",
    }

    if report_err:
        msg = f"Report load failed: {report_err} ({report_path})"
        log_path.write_text(msg + "\n", encoding="utf-8")
        results["error_excerpt"] = msg
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path:
        msg = "report.json missing python_path"
        log_path.write_text(msg + "\n", encoding="utf-8")
        results["error_excerpt"] = msg
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    results["reported_python_path"] = python_path
    python_exe = Path(python_path)
    if not (python_exe.exists() and os.access(str(python_exe), os.X_OK)):
        msg = f"python_path not executable: {python_path}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        results["error_excerpt"] = msg
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    rc, stdout, stderr = probe_env_paths(python_path)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[env_size] python_path={python_path}\n")
        if stdout:
            log.write("[env_size] stdout:\n" + stdout + "\n")
        if stderr:
            log.write("[env_size] stderr:\n" + stderr + "\n")

    if rc != 0 or not stdout.strip():
        results["error_excerpt"] = "Failed to probe env paths.\n" + stderr.strip()
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    try:
        payload = json.loads(stdout.strip().splitlines()[-1])
    except Exception as e:
        results["error_excerpt"] = f"Failed to parse probe JSON: {e}"
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    env_prefix = Path(payload.get("env_prefix", ""))
    site_packages = payload.get("site_packages", []) or []
    user_site = payload.get("user_site_packages", "")

    sizes: Dict[str, int] = {}
    warnings: List[str] = []

    env_bytes, env_warn = dir_size_bytes(env_prefix)
    sizes["env_prefix_bytes"] = env_bytes
    warnings.extend(env_warn)

    site_entries: List[Dict[str, Any]] = []
    site_total = 0
    for sp in site_packages:
        try:
            sp_path = Path(sp)
            sp_bytes, sp_warn = dir_size_bytes(sp_path)
            warnings.extend(sp_warn)
            site_total += sp_bytes
            site_entries.append({"path": str(sp_path), "size_bytes": sp_bytes})
        except Exception as e:
            warnings.append(f"site_packages_error:{sp}:{e}")

    if isinstance(user_site, str) and user_site:
        try:
            usp = Path(user_site)
            usp_bytes, usp_warn = dir_size_bytes(usp)
            warnings.extend(usp_warn)
            site_total += usp_bytes
            site_entries.append({"path": str(usp), "size_bytes": usp_bytes})
        except Exception as e:
            warnings.append(f"user_site_error:{user_site}:{e}")

    results["status"] = "success"
    results["skip_reason"] = "not_applicable"
    results["exit_code"] = 0
    results["observed"]["env_prefix"] = str(env_prefix)
    results["observed"]["env_prefix_size_MB"] = int(round(env_bytes / (1024 * 1024)))
    results["observed"]["site_packages"] = site_entries
    results["observed"]["site_packages_total_bytes"] = int(site_total)
    results["meta"]["warnings"] = warnings
    results["failure_category"] = "unknown"
    results["error_excerpt"] = ""

    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
