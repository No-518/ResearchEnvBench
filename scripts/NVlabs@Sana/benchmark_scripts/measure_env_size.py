#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import site
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail_lines(path: Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:]) if len(lines) > n else "\n".join(lines)
    except Exception:
        return ""


def git_commit(root: Path) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def resolve_report_path(cli_path: Optional[str]) -> Path:
    if cli_path:
        return Path(cli_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def read_report(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in files:
                p = Path(root) / name
                try:
                    total += p.stat().st_size
                except PermissionError:
                    warnings.append(f"permission_denied:{p}")
                except FileNotFoundError:
                    continue
                except Exception as e:
                    warnings.append(f"stat_error:{p}:{e}")
    except PermissionError:
        warnings.append(f"permission_denied:{path}")
    except FileNotFoundError:
        warnings.append(f"missing:{path}")
    except Exception as e:
        warnings.append(f"os_walk_error:{path}:{e}")
    return total


def query_env_paths(python_path: str) -> Dict[str, Any]:
    code = (
        "import json, sys, site; "
        "data = {"
        "'sys_executable': sys.executable, "
        "'sys_prefix': sys.prefix, "
        "'site_packages': site.getsitepackages() if hasattr(site, 'getsitepackages') else [], "
        "'user_site': site.getusersitepackages() if hasattr(site, 'getusersitepackages') else ''"
        "}; "
        "print(json.dumps(data))"
    )
    r = subprocess.run([python_path, "-c", code], capture_output=True, text=True, timeout=30, check=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or f"python_path failed: {python_path}")
    return json.loads(r.stdout)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-path", default=None)
    args = ap.parse_args(argv)

    root = repo_root()
    out_dir = root / "build_output" / "env_size"
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    started = time.time()
    report_path = resolve_report_path(args.report_path)

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    error_excerpt = ""

    observed: Dict[str, Any] = {}
    warnings: List[str] = []
    reported_python_path = ""

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[env_size] report_path={report_path}\n")
        python_path = ""
        try:
            if not report_path.exists():
                raise FileNotFoundError(str(report_path))
            report = read_report(report_path)
            python_path = str(report.get("python_path", "") or "")
            reported_python_path = python_path
            if not python_path:
                raise KeyError("python_path missing from report.json")
            if not Path(python_path).exists():
                raise FileNotFoundError(f"python_path does not exist: {python_path}")
            if not os.access(python_path, os.X_OK):
                raise PermissionError(f"python_path not executable: {python_path}")

            info = query_env_paths(python_path)
            env_prefix = Path(info["sys_prefix"])
            sp_paths: List[Path] = []
            for p in info.get("site_packages", []) or []:
                sp_paths.append(Path(p))
            user_site = info.get("user_site") or ""
            if user_site:
                sp_paths.append(Path(user_site))

            env_prefix_size = dir_size_bytes(env_prefix, warnings)
            sp_sizes = []
            sp_total = 0
            for p in sp_paths:
                sz = dir_size_bytes(p, warnings)
                sp_total += sz
                sp_sizes.append({"path": str(p), "size_bytes": sz})

            observed = {
                "env_prefix": str(env_prefix),
                "env_prefix_size_MB": int(round(env_prefix_size / (1024 * 1024))),
                "site_packages": sp_sizes,
                "site_packages_total_bytes": sp_total,
            }
            status = "success"
            exit_code = 0
            failure_category = ""
        except Exception as e:
            log.write(f"[env_size] error: {e}\n")
            failure_category = "env_size_failed"
        finally:
            if warnings:
                log.write("[env_size] warnings:\n")
                for w in warnings:
                    log.write(f"  - {w}\n")

    payload = {
        "status": status,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"{sys.executable} {Path(__file__).name}",
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "python": sys.version.split()[0],
            "git_commit": git_commit(root),
            "report_path": str(report_path),
            "warnings": warnings,
            "duration_sec": round(time.time() - started, 3),
            "decision_reason": "Measure recursive size of sys.prefix and discovered site-packages using python_path from report.json.",
        },
        "failure_category": failure_category,
        "error_excerpt": tail_lines(log_path) if status == "failure" else "",
    }
    tmp = results_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(results_path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
