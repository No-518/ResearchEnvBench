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


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception as e:
        return None, f"read_error: {e}"
    try:
        parsed = json.loads(raw)
    except Exception:
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "invalid_json"
    return parsed, None


def _default_assets() -> Dict[str, Any]:
    return {
        "dataset": {"path": "unknown", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        "model": {"path": "unknown", "source": "unknown", "version": "unknown", "sha256": "unknown"},
    }


def _tail(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    if len(lines) <= max_lines:
        return "\n".join(lines).strip()
    return "\n".join(lines[-max_lines:]).strip()


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), stderr=subprocess.DEVNULL, text=True)
        return out.strip()
    except Exception:
        return "unknown"


def _dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        stack = [path]
        while stack:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                try:
                                    total += entry.stat(follow_symlinks=False).st_size
                                except PermissionError:
                                    warnings.append(f"permission denied: {entry.path}")
                        except PermissionError:
                            warnings.append(f"permission denied: {entry.path}")
            except PermissionError:
                warnings.append(f"permission denied: {cur}")
            except FileNotFoundError:
                continue
    except Exception as e:
        warnings.append(f"size_walk_error({path}): {e}")
    return total


INFO_CODE = r"""
import json
import site
import sys

out = {
  "sys_prefix": sys.prefix,
  "site_packages": [],
  "user_site": None,
}
try:
  out["site_packages"] = list(site.getsitepackages())
except Exception:
  out["site_packages"] = []
try:
  out["user_site"] = site.getusersitepackages()
except Exception:
  out["user_site"] = None
print(json.dumps(out))
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None, help="Override report.json path")
    parser.add_argument("--timeout-sec", type=int, default=120)
    args = parser.parse_args()

    out_dir = REPO_ROOT / "build_output" / "env_size"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(msg.rstrip() + "\n")

    report_path = Path(args.report_path) if args.report_path else Path(os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH))
    report, report_err = _load_json(report_path)

    reported_python_path = None
    if report and isinstance(report.get("python_path"), str):
        reported_python_path = report.get("python_path")

    if report_err or not reported_python_path:
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"{sys.executable} benchmark_scripts/measure_env_size.py --report-path {report_path}",
            "timeout_sec": args.timeout_sec,
            "framework": "unknown",
            "reported_python_path": reported_python_path or "missing",
            "observed": {},
            "assets": _default_assets(),
            "meta": {
                "python": sys.executable,
                "git_commit": _git_commit(REPO_ROOT),
                "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
                "decision_reason": "Reads python_path from report.json and measures disk usage of sys.prefix and site-packages.",
                "timestamp_utc": _utc_timestamp(),
                "report_path": str(report_path),
                "error": report_err or "missing_report",
            },
            "failure_category": "env_size_failed",
            "error_excerpt": "missing or invalid report.json",
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(payload["error_excerpt"])
        return 1

    python_path = Path(reported_python_path)
    if not (python_path.is_file() and os.access(str(python_path), os.X_OK)):
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"{sys.executable} benchmark_scripts/measure_env_size.py --report-path {report_path}",
            "timeout_sec": args.timeout_sec,
            "framework": "unknown",
            "reported_python_path": reported_python_path,
            "observed": {},
            "assets": _default_assets(),
            "meta": {
                "python": sys.executable,
                "git_commit": _git_commit(REPO_ROOT),
                "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
                "decision_reason": "Reads python_path from report.json and measures disk usage of sys.prefix and site-packages.",
                "timestamp_utc": _utc_timestamp(),
                "report_path": str(report_path),
                "error": "python_path_not_executable",
            },
            "failure_category": "env_size_failed",
            "error_excerpt": f"python_path is not executable: {reported_python_path}",
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(payload["error_excerpt"])
        return 1

    log(f"[env_size] using python_path={reported_python_path}")
    r = subprocess.run(
        [reported_python_path, "-c", INFO_CODE],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if r.stdout:
        log(r.stdout)
    if r.stderr:
        log(r.stderr)

    try:
        info = json.loads((r.stdout or "{}").strip())
    except Exception as e:
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"{reported_python_path} -c <site info>",
            "timeout_sec": args.timeout_sec,
            "framework": "unknown",
            "reported_python_path": reported_python_path,
            "observed": {},
            "assets": _default_assets(),
            "meta": {
                "python": reported_python_path,
                "git_commit": _git_commit(REPO_ROOT),
                "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
                "decision_reason": "Reads python_path from report.json and measures disk usage of sys.prefix and site-packages.",
                "timestamp_utc": _utc_timestamp(),
                "report_path": str(report_path),
                "return_code": r.returncode,
                "parse_error": str(e),
            },
            "failure_category": "env_size_failed",
            "error_excerpt": _tail(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    warnings: List[str] = []
    env_prefix = Path(str(info.get("sys_prefix", "")))
    env_prefix_size = _dir_size_bytes(env_prefix, warnings) if env_prefix else 0

    site_paths: List[Path] = []
    raw_site = info.get("site_packages")
    if isinstance(raw_site, list):
        for p in raw_site:
            if isinstance(p, str) and p:
                site_paths.append(Path(p))
    user_site = info.get("user_site")
    if isinstance(user_site, str) and user_site:
        site_paths.append(Path(user_site))

    site_sizes = []
    total_site = 0
    for p in site_paths:
        size_b = _dir_size_bytes(p, warnings) if p.exists() else 0
        site_sizes.append({"path": str(p), "size_bytes": int(size_b)})
        total_site += int(size_b)

    observed = {
        "env_prefix": str(env_prefix),
        "env_prefix_size_MB": int(env_prefix_size / (1024 * 1024)),
        "site_packages": site_sizes,
        "site_packages_total_bytes": int(total_site),
    }

    payload = {
        "status": "success",
        "skip_reason": "not_applicable",
        "exit_code": 0,
        "stage": "env_size",
        "task": "measure",
        "command": f"{reported_python_path} -c <site info>",
        "timeout_sec": args.timeout_sec,
        "framework": "unknown",
        "reported_python_path": reported_python_path,
        "observed": observed,
        "assets": _default_assets(),
        "meta": {
            "python": reported_python_path,
            "git_commit": _git_commit(REPO_ROOT),
            "env_vars": {"SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", "")},
            "decision_reason": "Reads python_path from report.json and measures disk usage of sys.prefix and site-packages.",
            "timestamp_utc": _utc_timestamp(),
            "report_path": str(report_path),
            "return_code": r.returncode,
            "warnings": warnings,
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
