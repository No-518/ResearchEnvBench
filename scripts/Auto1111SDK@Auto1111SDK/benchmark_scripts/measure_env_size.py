#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True


REPO_ROOT = Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        sha = (cp.stdout or "").strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_report = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_report:
        return Path(env_report)
    return Path("/opt/scimlopsbench/report.json")


def _read_report(report_path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = report_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception as e:
        return None, f"missing_report: {type(e).__name__}: {e}"
    try:
        obj = json.loads(raw)
    except Exception as e:
        return None, f"invalid_json: {type(e).__name__}: {e}"
    if not isinstance(obj, dict):
        return None, "invalid_json: top-level is not an object"
    return obj, None


def _env_snapshot() -> Dict[str, str]:
    keys = [
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "XDG_CACHE_HOME",
        "PIP_CACHE_DIR",
        "CONDA_PKGS_DIRS",
    ]
    snap: Dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            snap[k] = v
    return snap


def _probe_env_paths(python_exe: str, timeout_sec: int = 60) -> Tuple[Optional[dict], Optional[str]]:
    code = r"""
import json
import site
import sys

out = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_packages": [],
  "user_site": None,
}

try:
  out["site_packages"] = list(site.getsitepackages())
except Exception as e:
  out["site_packages_error"] = f"{type(e).__name__}: {e}"

try:
  out["user_site"] = site.getusersitepackages()
except Exception as e:
  out["user_site_error"] = f"{type(e).__name__}: {e}"

print(json.dumps(out))
"""
    try:
        cp = subprocess.run(
            [python_exe, "-c", code],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if cp.returncode != 0:
        return None, (cp.stderr or cp.stdout or "").strip()[-4000:]
    try:
        obj = json.loads((cp.stdout or "").strip() or "{}")
    except Exception as e:
        return None, f"invalid_probe_json: {type(e).__name__}: {e}"
    if not isinstance(obj, dict):
        return None, "invalid_probe_json: top-level is not an object"
    return obj, None


def _dir_size_bytes(root: Path, warnings: List[str]) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            try:
                if path.is_symlink():
                    continue
                if path.is_file():
                    total += path.stat().st_size
            except PermissionError:
                warnings.append(f"permission_denied: {path}")
            except FileNotFoundError:
                continue
            except OSError as e:
                warnings.append(f"oserror: {path}: {type(e).__name__}: {e}")
    except PermissionError:
        warnings.append(f"permission_denied_root: {root}")
    except Exception as e:
        warnings.append(f"walk_error: {root}: {type(e).__name__}: {e}")
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args()

    out_dir = REPO_ROOT / "build_output" / "env_size"
    _safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    timeout_sec = 120
    base_assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }

    def log(line: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")

    report_path = _resolve_report_path(args.report_path)
    log(f"[env_size] start_utc={_utc_now_iso()}")
    log(f"[env_size] report_path={report_path}")

    report, report_err = _read_report(report_path)
    reported_python_path = (report or {}).get("python_path") if isinstance(report, dict) else None

    if report_err or not isinstance(reported_python_path, str) or not reported_python_path.strip():
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"{sys.executable} {Path(__file__).name} --report-path {report_path}",
            "timeout_sec": timeout_sec,
            "framework": "unknown",
            "assets": base_assets,
            "reported_python_path": reported_python_path or "",
            "observed": {},
            "meta": {
                "python": sys.executable,
                "git_commit": _git_commit(REPO_ROOT),
                "timestamp_utc": _utc_now_iso(),
                "env_vars": _env_snapshot(),
                "report_error": report_err,
                "decision_reason": "Measure environment size using python_path from agent report.",
            },
            "failure_category": "env_size_failed",
            "error_excerpt": f"report missing/invalid or python_path missing: {report_err}",
        }
        _write_json(results_path, payload)
        return 1

    python_exe = reported_python_path.strip()
    if not (os.path.exists(python_exe) and os.access(python_exe, os.X_OK)):
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"{sys.executable} {Path(__file__).name} --report-path {report_path}",
            "timeout_sec": timeout_sec,
            "framework": "unknown",
            "assets": base_assets,
            "reported_python_path": python_exe,
            "observed": {},
            "meta": {
                "python": sys.executable,
                "git_commit": _git_commit(REPO_ROOT),
                "timestamp_utc": _utc_now_iso(),
                "env_vars": _env_snapshot(),
                "decision_reason": "Measure environment size using python_path from agent report.",
            },
            "failure_category": "env_size_failed",
            "error_excerpt": f"python_path is not executable: {python_exe}",
        }
        _write_json(results_path, payload)
        return 1

    probe, probe_err = _probe_env_paths(python_exe)
    if probe_err or not probe:
        payload = {
            "status": "failure",
            "skip_reason": "not_applicable",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"{python_exe} -c <env_probe>",
            "timeout_sec": timeout_sec,
            "framework": "unknown",
            "assets": base_assets,
            "reported_python_path": python_exe,
            "observed": {},
            "meta": {
                "python": sys.executable,
                "git_commit": _git_commit(REPO_ROOT),
                "timestamp_utc": _utc_now_iso(),
                "env_vars": _env_snapshot(),
                "decision_reason": "Measure environment size using python_path from agent report.",
            },
            "failure_category": "env_size_failed",
            "error_excerpt": (probe_err or "unknown error")[-4000:],
        }
        _write_json(results_path, payload)
        return 1

    env_prefix = Path(str(probe.get("sys_prefix", "")))
    site_packages = []
    for p in probe.get("site_packages", []) or []:
        if isinstance(p, str) and p:
            site_packages.append(Path(p))
    user_site = probe.get("user_site")
    if isinstance(user_site, str) and user_site:
        site_packages.append(Path(user_site))

    warnings: List[str] = []
    env_prefix_size = _dir_size_bytes(env_prefix, warnings) if env_prefix.exists() else 0
    site_sizes = []
    site_total = 0
    for sp in site_packages:
        size = _dir_size_bytes(sp, warnings) if sp.exists() else 0
        site_sizes.append({"path": str(sp), "size_bytes": size})
        site_total += size

    payload = {
        "status": "success",
        "skip_reason": "not_applicable",
        "exit_code": 0,
        "stage": "env_size",
        "task": "measure",
        "command": f"{python_exe} -c <env_probe>",
        "timeout_sec": timeout_sec,
        "framework": "unknown",
        "assets": base_assets,
        "reported_python_path": python_exe,
        "observed": {
            "env_prefix": str(env_prefix),
            "env_prefix_size_MB": int(round(env_prefix_size / (1024 * 1024))),
            "site_packages": site_sizes,
            "site_packages_total_bytes": site_total,
        },
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(REPO_ROOT),
            "timestamp_utc": _utc_now_iso(),
            "env_vars": _env_snapshot(),
            "probe": probe,
            "warnings": warnings,
            "decision_reason": "Measure environment size using python_path from agent report.",
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }
    _write_json(results_path, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
