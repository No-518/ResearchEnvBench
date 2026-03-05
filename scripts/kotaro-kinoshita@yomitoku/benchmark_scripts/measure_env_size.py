#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-max_lines:] if len(lines) > max_lines else lines
        return "\n".join(tail)
    except Exception:
        return ""


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path("/opt/scimlopsbench/report.json")


def _safe_json_load(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid json in {path}: {e}"
    except Exception as e:
        return None, f"failed to read {path}: {e}"


def _dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    if not path.exists():
        warnings.append(f"missing_path: {path}")
        return 0
    try:
        for root, _dirs, files in os.walk(path, onerror=lambda e: warnings.append(f"os_walk_error:{path}:{e}")):
            for name in files:
                fp = Path(root) / name
                try:
                    total += fp.stat().st_size
                except PermissionError as e:
                    warnings.append(f"permission_error:{fp}:{e}")
                except FileNotFoundError:
                    continue
                except OSError as e:
                    warnings.append(f"os_error:{fp}:{e}")
    except PermissionError as e:
        warnings.append(f"permission_error:{path}:{e}")
    except OSError as e:
        warnings.append(f"os_error:{path}:{e}")
    return total


def _quote_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args()

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "env_size"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    log_lines: List[str] = []
    log_lines.append(f"[env_size] start_utc={_utc_now_iso()}")

    report_path = resolve_report_path(args.report_path)
    log_lines.append(f"[env_size] report_path={report_path}")

    report, err = _safe_json_load(report_path)
    if report is None:
        log_lines.append(f"[env_size] ERROR: {err}")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"python {Path(__file__).name} --report-path {report_path}",
            "reported_python_path": None,
            "observed": {},
            "meta": {"timestamp_utc": _utc_now_iso()},
            "failure_category": "env_size_failed",
            "error_excerpt": _tail_lines(log_path),
        }
        _write_json(results_path, payload)
        return 1

    python_path = report.get("python_path")
    if not isinstance(python_path, str) or not python_path.strip():
        log_lines.append('[env_size] ERROR: report.json missing "python_path"')
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"python {Path(__file__).name} --report-path {report_path}",
            "reported_python_path": python_path,
            "observed": {},
            "meta": {"timestamp_utc": _utc_now_iso()},
            "failure_category": "env_size_failed",
            "error_excerpt": _tail_lines(log_path),
        }
        _write_json(results_path, payload)
        return 1

    python_path = str(python_path)
    py_exec = Path(python_path)
    if not py_exec.exists():
        log_lines.append(f"[env_size] ERROR: python_path does not exist: {python_path}")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"python {Path(__file__).name} --report-path {report_path}",
            "reported_python_path": python_path,
            "observed": {},
            "meta": {"timestamp_utc": _utc_now_iso()},
            "failure_category": "env_size_failed",
            "error_excerpt": _tail_lines(log_path),
        }
        _write_json(results_path, payload)
        return 1

    probe_code = (
        "import json, sys, site; "
        "out={'sys_prefix': sys.prefix, 'site_packages': [], 'user_site': None}; "
        "try: out['site_packages']=site.getsitepackages();\n"
        "except Exception: out['site_packages']=[];\n"
        "try: out['user_site']=site.getusersitepackages();\n"
        "except Exception: out['user_site']=None;\n"
        "print(json.dumps(out))"
    )
    probe_cmd = [python_path, "-c", probe_code]
    log_lines.append(f"[env_size] probe_cmd={_quote_cmd(probe_cmd)}")

    try:
        proc = subprocess.run(
            probe_cmd,
            cwd=str(repo_root),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        log_lines.append(f"[env_size] ERROR: probe failed: {e}")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"python {Path(__file__).name} --report-path {report_path}",
            "reported_python_path": python_path,
            "observed": {},
            "meta": {"timestamp_utc": _utc_now_iso()},
            "failure_category": "env_size_failed",
            "error_excerpt": _tail_lines(log_path),
        }
        _write_json(results_path, payload)
        return 1

    if proc.stdout:
        log_lines.append("[env_size] --- probe stdout ---")
        log_lines.append(proc.stdout.rstrip("\n"))
    if proc.stderr:
        log_lines.append("[env_size] --- probe stderr ---")
        log_lines.append(proc.stderr.rstrip("\n"))

    probe_raw = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
    try:
        probe = json.loads(probe_raw) if probe_raw else {}
    except Exception as e:
        log_lines.append(f"[env_size] ERROR: probe output invalid json: {e}")
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": f"python {Path(__file__).name} --report-path {report_path}",
            "reported_python_path": python_path,
            "observed": {},
            "meta": {"timestamp_utc": _utc_now_iso()},
            "failure_category": "env_size_failed",
            "error_excerpt": _tail_lines(log_path),
        }
        _write_json(results_path, payload)
        return 1

    env_prefix = Path(str(probe.get("sys_prefix") or ""))
    site_packages: List[str] = []
    if isinstance(probe.get("site_packages"), list):
        site_packages.extend([str(x) for x in probe.get("site_packages") if isinstance(x, str)])
    user_site = probe.get("user_site")
    if isinstance(user_site, str) and user_site.strip():
        site_packages.append(user_site)

    warnings: List[str] = []
    env_prefix_size = _dir_size_bytes(env_prefix, warnings)
    site_sizes: List[Dict[str, Any]] = []
    site_total = 0
    for sp in site_packages:
        p = Path(sp)
        sz = _dir_size_bytes(p, warnings)
        site_total += sz
        site_sizes.append({"path": str(p), "size_bytes": int(sz)})

    observed = {
        "env_prefix": str(env_prefix),
        "env_prefix_size_MB": int(round(env_prefix_size / (1024 * 1024))),
        "site_packages": site_sizes,
        "site_packages_total_bytes": int(site_total),
    }

    payload = {
        "status": "success",
        "exit_code": 0,
        "stage": "env_size",
        "task": "measure",
        "command": f"python {Path(__file__).name} --report-path {report_path}",
        "reported_python_path": python_path,
        "observed": observed,
        "meta": {
            "timestamp_utc": _utc_now_iso(),
            "warnings": warnings,
        },
        "failure_category": "unknown",
        "error_excerpt": "",
    }

    log_lines.append(f"[env_size] env_prefix={env_prefix} size_bytes={env_prefix_size}")
    log_lines.append(f"[env_size] site_packages_total_bytes={site_total}")
    if warnings:
        log_lines.append("[env_size] warnings:")
        log_lines.extend([f"  - {w}" for w in warnings[:200]])

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    _write_json(results_path, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

