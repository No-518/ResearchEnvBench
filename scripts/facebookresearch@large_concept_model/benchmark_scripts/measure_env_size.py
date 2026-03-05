#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"missing: {path}"
    except Exception as e:
        return None, f"read_error: {path}: {e}"
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, f"invalid_json_root_not_object: {path}"
        return data, None
    except Exception as e:
        return None, f"invalid_json: {path}: {e}"


def _default_assets() -> Dict[str, Dict[str, str]]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _safe_walk_size_bytes(root: Path, warnings: List[str]) -> int:
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    if p.is_symlink():
                        continue
                    total += p.stat().st_size
                except PermissionError:
                    warnings.append(f"permission_denied: {p}")
                except FileNotFoundError:
                    continue
                except Exception as e:
                    warnings.append(f"stat_error: {p}: {type(e).__name__}: {e}")
    except PermissionError:
        warnings.append(f"permission_denied_walk_root: {root}")
    except Exception as e:
        warnings.append(f"walk_error: {root}: {type(e).__name__}: {e}")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure environment disk usage for the reported python_path.")
    parser.add_argument("--report-path", default=None, help="Override report.json path.")
    args = parser.parse_args()

    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "env_size"
    _ensure_dir(stage_dir)

    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    report_path = Path(
        args.report_path
        or os.environ.get("SCIMLOPSBENCH_REPORT")
        or "/opt/scimlopsbench/report.json"
    )

    command = f"{sys.executable} {Path(__file__).name} --report-path {report_path}"

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

    warnings: List[str] = []

    report, rep_err = _read_json(report_path)
    python_path = ""
    if report is None:
        error_excerpt = f"Missing/invalid report.json: {rep_err}"
    else:
        python_path = str(report.get("python_path") or "")
        if not python_path:
            error_excerpt = "report.json is missing python_path"
        else:
            py = Path(python_path)
            if not (py.is_file() and os.access(str(py), os.X_OK)):
                error_excerpt = f"python_path is not executable: {python_path}"
            else:
                # Ask the reported python for sys.prefix and site-packages locations.
                probe = (
                    "import json, sys, site; "
                    "out={'sys_executable': sys.executable, 'sys_prefix': sys.prefix, "
                    "'site_packages': site.getsitepackages() if hasattr(site,'getsitepackages') else [], "
                    "'user_site': site.getusersitepackages() if hasattr(site,'getusersitepackages') else ''}; "
                    "print(json.dumps(out))"
                )
                try:
                    cp = subprocess.run(
                        [python_path, "-c", probe],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                        timeout=60,
                    )
                except Exception as e:
                    error_excerpt = f"Failed to run reported python_path: {type(e).__name__}: {e}"
                else:
                    if cp.returncode != 0:
                        error_excerpt = (cp.stderr or cp.stdout or "").strip()[-2000:]
                    else:
                        try:
                            info = json.loads(cp.stdout)
                        except Exception as e:
                            error_excerpt = f"Failed to parse python probe JSON: {type(e).__name__}: {e}"
                        else:
                            env_prefix = Path(str(info.get("sys_prefix") or ""))
                            site_pkgs = []
                            for p in info.get("site_packages") or []:
                                if isinstance(p, str) and p:
                                    site_pkgs.append(p)
                            user_site = info.get("user_site")
                            if isinstance(user_site, str) and user_site:
                                site_pkgs.append(user_site)

                            env_size_bytes = _safe_walk_size_bytes(env_prefix, warnings) if env_prefix else 0
                            site_entries: List[Dict[str, Any]] = []
                            site_total = 0
                            for sp in site_pkgs:
                                sp_path = Path(sp)
                                size = _safe_walk_size_bytes(sp_path, warnings) if sp_path.exists() else 0
                                site_entries.append({"path": str(sp_path), "size_bytes": size})
                                site_total += size

                            observed = {
                                "env_prefix": str(env_prefix),
                                "env_prefix_size_MB": int(env_size_bytes / (1024 * 1024)),
                                "site_packages": site_entries,
                                "site_packages_total_bytes": site_total,
                            }
                            status = "success"
                            exit_code = 0
                            failure_category = "unknown"

    with log_path.open("w", encoding="utf-8") as f:
        f.write(f"[env_size] report_path={report_path}\n")
        f.write(f"[env_size] reported_python_path={python_path}\n")
        f.write(f"[env_size] runner_python={sys.executable}\n")
        f.write(f"[env_size] runner_python_version={platform.python_version()}\n")
        f.write(f"[env_size] status={status}\n")
        if warnings:
            f.write("[env_size] warnings:\n")
            for w in warnings:
                f.write(f"  - {w}\n")
        if error_excerpt:
            f.write(f"[env_size] error_excerpt={error_excerpt}\n")

    payload: Dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": command,
        "reported_python_path": python_path,
        "observed": observed,
        "meta": {
            "python": sys.executable,
            "python_version": platform.python_version(),
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
                "SCIMLOPSBENCH_PYTHON": os.environ.get("SCIMLOPSBENCH_PYTHON", ""),
            },
            "warnings": warnings,
            "decision_reason": "Measure disk usage under sys.prefix and site-packages paths from the reported python interpreter.",
        },
        "assets": _default_assets(),
        "failure_category": failure_category if exit_code == 1 else "unknown",
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

