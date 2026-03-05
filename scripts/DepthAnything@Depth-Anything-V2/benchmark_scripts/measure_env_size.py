#!/usr/bin/env python3
import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def tail(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-max_lines:])


def try_git_commit(root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def resolve_report_path(cli_path: Optional[str]) -> str:
    if cli_path:
        return cli_path
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return env_path
    return DEFAULT_REPORT_PATH


def load_report(report_path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    p = Path(report_path)
    if not p.exists():
        return None, f"report_not_found: {report_path}"
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, f"report_invalid_json: {report_path}: {e}"


@dataclass
class ObservedEnv:
    env_prefix: str
    site_packages: List[str]


def query_env_paths(python_path: str, timeout_sec: int = 60) -> Tuple[Optional[ObservedEnv], Optional[str]]:
    code = r"""
import json
import site
import sys
payload = {
  "sys_prefix": sys.prefix,
  "site_packages": list(dict.fromkeys([p for p in (site.getsitepackages() if hasattr(site, "getsitepackages") else []) if p])),
  "user_site": site.getusersitepackages() if hasattr(site, "getusersitepackages") else "",
}
print(json.dumps(payload))
"""
    try:
        cp = subprocess.run(
            [python_path, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except Exception as e:
        return None, f"python_subprocess_failed: {e}"

    if cp.returncode != 0:
        return None, f"python_subprocess_nonzero: {cp.returncode}: {cp.stderr.strip()}"

    try:
        payload = json.loads(cp.stdout.strip())
    except Exception as e:
        return None, f"python_subprocess_invalid_json: {e}"

    prefix = str(payload.get("sys_prefix", ""))
    site_pkgs = []
    for p in payload.get("site_packages", []) or []:
        if isinstance(p, str) and p:
            site_pkgs.append(p)
    user_site = payload.get("user_site")
    if isinstance(user_site, str) and user_site:
        site_pkgs.append(user_site)
    # de-dup
    site_pkgs = list(dict.fromkeys(site_pkgs))
    return ObservedEnv(env_prefix=prefix, site_packages=site_pkgs), None


def dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        total += dir_size_bytes(Path(entry.path), warnings)
                    elif entry.is_file(follow_symlinks=False):
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except PermissionError:
                            warnings.append(f"permission_denied_file: {entry.path}")
                        except FileNotFoundError:
                            continue
                except PermissionError:
                    warnings.append(f"permission_denied_entry: {entry.path}")
                except FileNotFoundError:
                    continue
    except PermissionError:
        warnings.append(f"permission_denied_dir: {path}")
    except FileNotFoundError:
        return 0
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure python environment size for sys.prefix and site-packages.")
    ap.add_argument("--report-path", default=None, help="Path to /opt/scimlopsbench/report.json")
    args = ap.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report, report_err = load_report(report_path)

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    reported_python_path = ""
    warnings: List[str] = []
    observed: Dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
    }

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[env_size] timestamp_utc={utc_now_iso()}\n")
        log_f.write(f"[env_size] report_path={report_path}\n")

        if report is None:
            log_f.write(f"[env_size] report_error={report_err}\n")
        else:
            reported_python_path = str(report.get("python_path") or "")
            log_f.write(f"[env_size] reported_python_path={reported_python_path}\n")

        python_path_ok = bool(reported_python_path) and Path(reported_python_path).exists() and os.access(reported_python_path, os.X_OK)
        if not python_path_ok:
            log_f.write("[env_size] python_path invalid or not executable.\n")
        else:
            env_info, env_err = query_env_paths(reported_python_path, timeout_sec=60)
            if env_info is None:
                log_f.write(f"[env_size] failed_query_env_paths={env_err}\n")
            else:
                observed["env_prefix"] = env_info.env_prefix
                observed["site_packages"] = []
                observed_prefix = Path(env_info.env_prefix) if env_info.env_prefix else None
                if observed_prefix and observed_prefix.exists():
                    prefix_bytes = dir_size_bytes(observed_prefix, warnings)
                    observed["env_prefix_size_MB"] = int(round(prefix_bytes / (1024 * 1024)))
                else:
                    warnings.append(f"env_prefix_missing: {env_info.env_prefix}")

                site_total = 0
                for p in env_info.site_packages:
                    pth = Path(p)
                    size_b = 0
                    if pth.exists():
                        size_b = dir_size_bytes(pth, warnings)
                    else:
                        warnings.append(f"site_packages_missing: {p}")
                    observed["site_packages"].append({"path": p, "size_bytes": size_b})
                    site_total += size_b
                observed["site_packages_total_bytes"] = site_total

                status = "success"
                exit_code = 0
                failure_category = ""

        if warnings:
            log_f.write("[env_size] warnings:\n")
            for w in warnings[:200]:
                log_f.write(f"  - {w}\n")

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
            "python": f"{sys.executable} ({platform.python_version()})",
            "git_commit": try_git_commit(root),
            "timestamp_utc": utc_now_iso(),
            "report_path": report_path,
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
                "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            },
            "warnings": warnings,
        },
        "failure_category": failure_category,
        "error_excerpt": tail(log_path),
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
