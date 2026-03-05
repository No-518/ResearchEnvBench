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


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tail_lines(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(txt.splitlines()[-max_lines:])
    except Exception:
        return ""

def empty_assets() -> Dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def load_assets(repo: Path) -> Dict[str, Any]:
    p = repo / "build_output" / "prepare" / "results.json"
    if not p.exists():
        return empty_assets()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        assets = d.get("assets")
        return assets if isinstance(assets, dict) else empty_assets()
    except Exception:
        return empty_assets()


def dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            if p.is_symlink():
                continue
            if p.is_file():
                try:
                    total += p.stat().st_size
                except Exception as e:
                    warnings.append(f"stat_failed:{p}:{e}")
                continue
            if p.is_dir():
                try:
                    with os.scandir(p) as it:
                        for entry in it:
                            stack.append(Path(entry.path))
                except PermissionError as e:
                    warnings.append(f"permission_denied:{p}:{e}")
                except FileNotFoundError:
                    continue
                except Exception as e:
                    warnings.append(f"scandir_failed:{p}:{e}")
        except Exception as e:
            warnings.append(f"walk_failed:{p}:{e}")
    return total


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except Exception as e:
        return None, f"invalid_json: {e}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=os.environ.get("SCIMLOPSBENCH_REPORT", DEFAULT_REPORT_PATH))
    args = ap.parse_args(argv)

    repo = repo_root()
    out_dir = repo / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    reported_python_path = ""
    assets = load_assets(repo)
    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    error_excerpt = ""
    observed: Dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
    }

    def write_results() -> None:
        payload = {
            "status": status,
            "skip_reason": "unknown",
            "exit_code": exit_code,
            "stage": "env_size",
            "task": "measure",
            "command": f"python benchmark_scripts/measure_env_size.py --report-path {args.report_path}",
            "timeout_sec": 120,
            "framework": "unknown",
            "assets": assets,
            "reported_python_path": reported_python_path,
            "observed": observed,
            "meta": {
                "python": sys.executable,
                "git_commit": "",
                "env_vars": {
                    "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
                },
                "decision_reason": "Measure disk usage of sys.prefix and site-packages for the report python environment.",
                "timestamp_utc": utcnow(),
            },
            "failure_category": failure_category,
            "error_excerpt": error_excerpt,
        }
        results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with log_path.open("w", encoding="utf-8") as log:
        try:
            report_path = Path(args.report_path)
            report, err = load_json(report_path)
            if report is None:
                log.write(f"[env_size] report read failed: {err}\n")
                error_excerpt = f"missing/invalid report: {err}"
                write_results()
                return 1

            py = report.get("python_path")
            if not isinstance(py, str) or not py.strip():
                log.write("[env_size] report missing python_path\n")
                error_excerpt = "report missing python_path"
                write_results()
                return 1

            reported_python_path = py.strip()
            if not (Path(reported_python_path).exists() and os.access(reported_python_path, os.X_OK)):
                log.write(f"[env_size] python_path not executable: {reported_python_path}\n")
                error_excerpt = f"python_path not executable: {reported_python_path}"
                write_results()
                return 1

            info_code = r"""
import json, site, sys
out = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "sitepackages": site.getsitepackages() if hasattr(site, "getsitepackages") else [],
  "usersite": site.getusersitepackages() if hasattr(site, "getusersitepackages") else "",
}
print(json.dumps(out))
""".strip()

            cmd = [reported_python_path, "-c", info_code]
            log.write(f"[env_size] probing: {reported_python_path} -c <code>\n")
            log.flush()

            proc = subprocess.run(
                cmd,
                cwd=str(repo),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
            )
            log.write(proc.stdout or "")
            log.flush()

            info = json.loads((proc.stdout or "").strip().splitlines()[-1])
            env_prefix = str(info.get("sys_prefix") or "")
            sitepackages = info.get("sitepackages") or []
            usersite = str(info.get("usersite") or "")

            warnings: List[str] = []
            env_prefix_path = Path(env_prefix) if env_prefix else Path()
            env_size = dir_size_bytes(env_prefix_path, warnings) if env_prefix else 0

            site_entries: List[Dict[str, Any]] = []
            total_site = 0
            for sp in list(sitepackages) + ([usersite] if usersite else []):
                try:
                    sp_path = Path(sp)
                    sp_warnings: List[str] = []
                    sp_size = dir_size_bytes(sp_path, sp_warnings) if sp and sp_path.exists() else 0
                    total_site += sp_size
                    if sp_warnings:
                        warnings.extend(sp_warnings)
                    site_entries.append({"path": str(sp_path), "size_bytes": sp_size})
                except Exception as e:
                    warnings.append(f"sitepackages_failed:{sp}:{e}")

            observed.update(
                {
                    "env_prefix": env_prefix,
                    "env_prefix_size_MB": int(round(env_size / (1024 * 1024))),
                    "site_packages": site_entries,
                    "site_packages_total_bytes": int(total_site),
                    "warnings": warnings,
                }
            )

            status = "success"
            exit_code = 0
            failure_category = "unknown"
            error_excerpt = ""
        except Exception as e:
            log.write(f"[env_size] exception: {e}\n")
            error_excerpt = tail_lines(log_path, 220) or repr(e)
        finally:
            write_results()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
