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
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_report_path(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def tail_text(path: Path, n: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except Exception:
        return ""


def cmd_str(cmd: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def read_report(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception:
        return None, "missing_report"
    try:
        data = json.loads(raw)
    except Exception:
        return None, "invalid_json"
    if not isinstance(data, dict):
        return None, "invalid_json"
    return data, None


def dir_size_bytes(path: Path, warnings: list[str]) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in files:
                fp = Path(root) / name
                try:
                    total += fp.stat().st_size
                except PermissionError:
                    warnings.append(f"PermissionError: {fp}")
                except FileNotFoundError:
                    continue
                except OSError as e:
                    warnings.append(f"OSError: {fp}: {e}")
    except PermissionError:
        warnings.append(f"PermissionError walking: {path}")
    except OSError as e:
        warnings.append(f"OSError walking: {path}: {e}")
    return total


INSPECT_SNIPPET = r"""
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

print(json.dumps(out, ensure_ascii=False))
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure environment size based on report.json python_path.")
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    # Fresh log
    log_f = log_path.open("w", encoding="utf-8")

    def log(msg: str) -> None:
        log_f.write(msg + "\n")
        log_f.flush()
        print(msg)

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    reported_python_path = ""
    observed: dict[str, Any] = {}
    warnings: list[str] = []

    report_path = resolve_report_path(args.report_path)
    log(f"[env_size] start_utc={utc_now_iso()}")
    log(f"[env_size] report_path={report_path}")

    report, rep_err = read_report(report_path)
    if rep_err is not None:
        log(f"[env_size] ERROR: report read failed: {rep_err}")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": cmd_str([sys.executable, str(Path(__file__).name)] + sys.argv[1:]),
            "reported_python_path": "",
            "observed": {},
            "meta": {
                "git_commit": "",
                "timestamp_utc": utc_now_iso(),
                "warnings": [],
            },
            "failure_category": "env_size_failed",
            "error_excerpt": tail_text(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log_f.close()
        return 1

    reported_python_path = str(report.get("python_path") or "").strip()
    if not reported_python_path:
        log("[env_size] ERROR: report missing python_path")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": cmd_str([sys.executable, str(Path(__file__).name)] + sys.argv[1:]),
            "reported_python_path": "",
            "observed": {},
            "meta": {
                "git_commit": "",
                "timestamp_utc": utc_now_iso(),
                "warnings": [],
            },
            "failure_category": "env_size_failed",
            "error_excerpt": tail_text(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log_f.close()
        return 1

    py = Path(reported_python_path)
    if not py.exists() or not os.access(py, os.X_OK):
        log(f"[env_size] ERROR: python_path not executable: {reported_python_path}")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": cmd_str([sys.executable, str(Path(__file__).name)] + sys.argv[1:]),
            "reported_python_path": reported_python_path,
            "observed": {},
            "meta": {
                "git_commit": "",
                "timestamp_utc": utc_now_iso(),
                "warnings": [],
            },
            "failure_category": "env_size_failed",
            "error_excerpt": tail_text(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log_f.close()
        return 1

    cmd = [reported_python_path, "-c", INSPECT_SNIPPET]
    log(f"[env_size] cmd={cmd_str(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        log("[env_size] ERROR: timeout while inspecting env paths")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": cmd_str([sys.executable, str(Path(__file__).name)] + sys.argv[1:]),
            "reported_python_path": reported_python_path,
            "observed": {},
            "meta": {"timestamp_utc": utc_now_iso(), "warnings": warnings},
            "failure_category": "env_size_failed",
            "error_excerpt": tail_text(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log_f.close()
        return 1

    if proc.stderr:
        log("[env_size] stderr:\n" + proc.stderr.strip())
    stdout = (proc.stdout or "").strip()
    log("[env_size] stdout:\n" + stdout)

    try:
        obs = json.loads(stdout) if stdout else {}
    except Exception:
        obs = {}

    env_prefix = str(obs.get("sys_prefix") or "")
    site_packages = obs.get("site_packages") if isinstance(obs.get("site_packages"), list) else []
    user_site = obs.get("user_site")
    if isinstance(user_site, str) and user_site:
        site_packages = list(site_packages) + [user_site]

    env_prefix_path = Path(env_prefix) if env_prefix else None
    if env_prefix_path is None or not env_prefix_path.exists():
        log(f"[env_size] ERROR: sys.prefix invalid: {env_prefix}")
        payload = {
            "status": "failure",
            "exit_code": 1,
            "stage": "env_size",
            "task": "measure",
            "command": cmd_str([sys.executable, str(Path(__file__).name)] + sys.argv[1:]),
            "reported_python_path": reported_python_path,
            "observed": {},
            "meta": {"timestamp_utc": utc_now_iso(), "warnings": warnings},
            "failure_category": "env_size_failed",
            "error_excerpt": tail_text(log_path),
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log_f.close()
        return 1

    env_size = dir_size_bytes(env_prefix_path, warnings)
    site_entries: list[dict[str, Any]] = []
    site_total = 0
    for sp in site_packages:
        sp_path = Path(sp)
        if not sp_path.exists():
            continue
        sz = dir_size_bytes(sp_path, warnings)
        site_entries.append({"path": str(sp_path), "size_bytes": sz})
        site_total += sz

    observed = {
        "env_prefix": str(env_prefix_path),
        "env_prefix_size_MB": int(round(env_size / (1024 * 1024))),
        "site_packages": site_entries,
        "site_packages_total_bytes": site_total,
    }

    status = "success"
    exit_code = 0

    payload = {
        "status": status,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": cmd_str([sys.executable, str(Path(__file__).name)] + sys.argv[1:]),
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "timestamp_utc": utc_now_iso(),
            "warnings": warnings,
        },
        "failure_category": "unknown",
        "error_excerpt": tail_text(log_path),
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_f.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

