#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).strip()
        return out
    except Exception:
        return ""


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tail(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-max_lines:])
    except Exception:
        return ""


def _load_report(report_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not report_path.exists():
        return None, f"report not found at {report_path}"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "report JSON is not an object"
        return data, None
    except Exception as e:
        return None, f"failed to read/parse report: {e}"


def _is_executable(path: str) -> bool:
    p = Path(path)
    return p.exists() and p.is_file() and os.access(str(p), os.X_OK)


def _dir_size_bytes(root: Path, warnings: List[str]) -> int:
    total = 0
    try:
        for p in root.rglob("*"):
            try:
                if p.is_symlink():
                    continue
                if p.is_file():
                    total += p.stat().st_size
            except PermissionError:
                warnings.append(f"permission_denied: {p}")
            except FileNotFoundError:
                # racing deletions
                continue
            except OSError as e:
                warnings.append(f"oserror: {p}: {e}")
    except PermissionError:
        warnings.append(f"permission_denied_root: {root}")
    except FileNotFoundError:
        warnings.append(f"missing_root: {root}")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure environment size from python_path in report.json.")
    parser.add_argument("--report-path", default="")
    args = parser.parse_args()

    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "env_size"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    report_path = Path(
        args.report_path
        or os.environ.get("SCIMLOPSBENCH_REPORT")
        or "/opt/scimlopsbench/report.json"
    )

    base: Dict[str, Any] = {
        "status": "failure",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).name))} --report-path {shlex.quote(str(report_path))}",
        "reported_python_path": "",
        "observed": {
            "env_prefix": "",
            "env_prefix_size_MB": 0,
            "site_packages": [],
            "site_packages_total_bytes": 0,
        },
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_now_iso(),
            "env_vars": {"report_path": str(report_path)},
            "warnings": [],
        },
        "failure_category": "env_size_failed",
        "error_excerpt": "",
    }

    report, report_err = _load_report(report_path)
    if report is None:
        msg = f"Missing/invalid report: {report_err}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        base["error_excerpt"] = msg
        _write_json(results_path, base)
        return 1

    python_path = report.get("python_path")
    if not python_path or not isinstance(python_path, str):
        msg = "report.python_path missing/invalid"
        log_path.write_text(msg + "\n", encoding="utf-8")
        base["error_excerpt"] = msg
        _write_json(results_path, base)
        return 1

    base["reported_python_path"] = python_path
    if not _is_executable(python_path):
        msg = f"python_path not executable: {python_path}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        base["error_excerpt"] = msg
        _write_json(results_path, base)
        return 1

    probe_cmd = [
        python_path,
        "-c",
        r"""
import json, site, sys
data = {
  "sys_prefix": sys.prefix,
  "site_packages": [],
}
try:
  data["site_packages"] = site.getsitepackages()
except Exception:
  data["site_packages"] = []
try:
  data["user_site"] = site.getusersitepackages()
except Exception:
  data["user_site"] = ""
print(json.dumps(data))
""",
    ]
    with log_path.open("w", encoding="utf-8", errors="replace") as log_f:
        log_f.write("probe_cmd: " + " ".join(shlex.quote(x) for x in probe_cmd) + "\n")
        try:
            proc = subprocess.run(
                probe_cmd,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            base["error_excerpt"] = "env probe timed out"
            _write_json(results_path, base)
            return 1

        log_f.write((proc.stdout or "") + "\n")
        if proc.stderr:
            log_f.write(proc.stderr + "\n")
        if proc.returncode != 0:
            base["error_excerpt"] = _tail(log_path)
            _write_json(results_path, base)
            return 1

    try:
        info = json.loads(proc.stdout.strip() or "{}")
    except Exception as e:
        base["error_excerpt"] = f"failed to parse env probe JSON: {e}\n{_tail(log_path)}"
        _write_json(results_path, base)
        return 1

    env_prefix = Path(str(info.get("sys_prefix") or "")).resolve()
    site_packages: List[str] = []
    for p in info.get("site_packages") or []:
        if isinstance(p, str) and p:
            site_packages.append(p)
    user_site = info.get("user_site")
    if isinstance(user_site, str) and user_site:
        site_packages.append(user_site)

    warnings: List[str] = []
    env_prefix_size = _dir_size_bytes(env_prefix, warnings)
    site_entries: List[Dict[str, Any]] = []
    site_total = 0
    for sp in site_packages:
        sp_path = Path(sp).resolve()
        sp_size = _dir_size_bytes(sp_path, warnings)
        site_entries.append({"path": str(sp_path), "size_bytes": sp_size})
        site_total += sp_size

    base["observed"] = {
        "env_prefix": str(env_prefix),
        "env_prefix_size_MB": int(env_prefix_size / (1024 * 1024)),
        "site_packages": site_entries,
        "site_packages_total_bytes": site_total,
    }
    base["meta"]["warnings"] = warnings[:2000]

    base["status"] = "success"
    base["exit_code"] = 0
    base["failure_category"] = "unknown"
    base["error_excerpt"] = ""
    _write_json(results_path, base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

