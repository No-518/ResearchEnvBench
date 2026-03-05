#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-max_lines:])
    except Exception:
        return ""


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, f"Expected JSON object in {path}"
        return data, None
    except FileNotFoundError:
        return None, f"Missing report: {path}"
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON in {path}: {e}"
    except Exception as e:
        return None, f"Failed to read {path}: {e}"


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return DEFAULT_REPORT_PATH


def git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def is_executable(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(path, os.X_OK)
    except Exception:
        return False


def dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in files:
                p = Path(root) / name
                try:
                    total += p.stat().st_size
                except PermissionError as e:
                    warnings.append(f"permission_error: {p}: {e}")
                except FileNotFoundError:
                    continue
                except OSError as e:
                    warnings.append(f"os_error: {p}: {e}")
    except PermissionError as e:
        warnings.append(f"permission_error_walk: {path}: {e}")
    except Exception as e:
        warnings.append(f"walk_error: {path}: {e}")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure environment size for python_path from agent report.")
    parser.add_argument("--report-path", type=str, default=None)
    parser.add_argument("--out-root", type=str, default="build_output")
    args = parser.parse_args()

    out_root = (REPO_ROOT / args.out_root).resolve()
    stage_dir = out_root / "env_size"
    ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    report_path = resolve_report_path(args.report_path)
    report, report_err = read_json(report_path)

    payload: Dict[str, Any] = {
        "status": "failure",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": f"python {Path(__file__).name} --report-path {report_path}",
        "reported_python_path": "",
        "observed": {
            "env_prefix": "",
            "env_prefix_size_MB": 0,
            "site_packages": [],
            "site_packages_total_bytes": 0,
        },
        "meta": {
            "git_commit": git_commit(),
            "timestamp_utc": utc_now_iso(),
            "report_path": str(report_path),
            "warnings": [],
            "env_vars": {
                k: os.environ.get(k)
                for k in ["SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON"]
                if os.environ.get(k) is not None
            },
        },
        "failure_category": "env_size_failed",
        "error_excerpt": "",
    }

    if report is None:
        log_path.write_text(f"[{utc_now_iso()}] {report_err}\n", encoding="utf-8")
        payload["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    python_path = report.get("python_path")
    payload["reported_python_path"] = str(python_path or "")
    if not python_path:
        log_path.write_text(f"[{utc_now_iso()}] report missing python_path\n", encoding="utf-8")
        payload["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    py = Path(str(python_path))
    if not is_executable(py):
        log_path.write_text(f"[{utc_now_iso()}] python_path not executable: {py}\n", encoding="utf-8")
        payload["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    snippet = r"""
import json, sys, site
out = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_packages": list(dict.fromkeys([p for p in (site.getsitepackages() or []) if p])),
  "user_site": site.getusersitepackages(),
}
print(json.dumps(out))
"""

    try:
        proc = subprocess.run([str(py), "-c", snippet], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    except Exception as e:
        log_path.write_text(f"[{utc_now_iso()}] failed to run python_path: {e}\n", encoding="utf-8")
        payload["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    log_path.write_text(
        f"[{utc_now_iso()}] python_path={py}\n"
        f"[{utc_now_iso()}] returncode={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}\n",
        encoding="utf-8",
    )

    if proc.returncode != 0:
        payload["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    try:
        observed = json.loads(proc.stdout.strip() or "{}")
    except Exception as e:
        payload["meta"]["warnings"].append(f"failed_to_parse_python_probe: {e}")
        payload["error_excerpt"] = tail_text(log_path)
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    warnings: List[str] = []
    env_prefix = Path(str(observed.get("sys_prefix") or ""))
    site_paths: List[Path] = []
    for p in observed.get("site_packages") or []:
        site_paths.append(Path(str(p)))
    user_site = observed.get("user_site")
    if user_site:
        site_paths.append(Path(str(user_site)))

    env_prefix_size = dir_size_bytes(env_prefix, warnings) if env_prefix.exists() else 0
    site_entries = []
    site_total = 0
    for p in site_paths:
        if not p.exists():
            warnings.append(f"missing_site_path: {p}")
            continue
        size = dir_size_bytes(p, warnings)
        site_entries.append({"path": str(p), "size_bytes": int(size)})
        site_total += int(size)

    payload["status"] = "success"
    payload["exit_code"] = 0
    payload["failure_category"] = ""
    payload["observed"] = {
        "env_prefix": str(env_prefix),
        "env_prefix_size_MB": int(round(env_prefix_size / (1024 * 1024))),
        "site_packages": site_entries,
        "site_packages_total_bytes": int(site_total),
    }
    payload["meta"]["warnings"] = warnings
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

