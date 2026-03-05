#!/usr/bin/env python3
import argparse
import json
import os
import site
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True, timeout=10)
            .strip()
        )
    except Exception:
        return ""


def resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path("/opt/scimlopsbench/report.json")


def is_executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def safe_read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing_report:{path}"
    except Exception as e:
        return None, f"invalid_json:{path}:{type(e).__name__}:{e}"


def recursive_size_bytes(root: Path) -> Tuple[int, List[str]]:
    total = 0
    warnings: List[str] = []
    stack: List[Path] = [root]
    while stack:
        p = stack.pop()
        try:
            if p.is_symlink():
                continue
            if p.is_file():
                try:
                    total += p.stat().st_size
                except Exception as e:
                    warnings.append(f"stat_failed:{p}:{type(e).__name__}:{e}")
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
                    warnings.append(f"scandir_failed:{p}:{type(e).__name__}:{e}")
        except Exception as e:
            warnings.append(f"walk_failed:{p}:{type(e).__name__}:{e}")
    return total, warnings


def python_probe_env(python_path: str, timeout_sec: int = 60) -> Tuple[Optional[Dict[str, Any]], str]:
    code = r"""
import json, site, sys
out = {
  "sys_prefix": sys.prefix,
  "site_packages": [],
  "user_site": "",
}
try:
  out["site_packages"] = list(site.getsitepackages())
except Exception:
  out["site_packages"] = []
try:
  out["user_site"] = site.getusersitepackages() or ""
except Exception:
  out["user_site"] = ""
print(json.dumps(out))
"""
    proc = subprocess.run(
        [python_path, "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=float(timeout_sec),
    )
    raw = (proc.stdout or "").strip()
    try:
        obj = json.loads(raw.splitlines()[-1]) if raw else None
        return obj, raw
    except Exception:
        return None, raw


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default="")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    root = repo_root()
    out_dir = Path(args.out_dir) if args.out_dir else (root / "build_output" / "env_size")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path or None)
    report, report_err = safe_read_json(report_path)

    reported_python_path = ""
    if report and isinstance(report, dict):
        reported_python_path = str(report.get("python_path", "") or "")

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    error_excerpt = ""

    observed: Dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
        "warnings": [],
    }

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"[env_size] timestamp_utc={utc_timestamp()}\n")
        logf.write(f"[env_size] report_path={report_path}\n")
        logf.write(f"[env_size] reported_python_path={reported_python_path}\n")

        if not report or report_err:
            logf.write(f"[env_size] report_error={report_err}\n")
            error_excerpt = report_err or "missing_report"
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "env_size",
                "task": "measure",
                "command": f"python benchmark_scripts/measure_env_size.py --report-path {report_path}",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "reported_python_path": reported_python_path,
                "observed": observed,
                "meta": {"python": reported_python_path, "git_commit": git_commit(root), "timestamp_utc": utc_timestamp()},
                "failure_category": "env_size_failed",
                "error_excerpt": error_excerpt,
            }
            write_json(results_path, payload)
            return 1

        py = Path(reported_python_path)
        if not reported_python_path or not is_executable(py):
            msg = f"python_path missing or not executable: {reported_python_path}"
            logf.write(f"[env_size] {msg}\n")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "env_size",
                "task": "measure",
                "command": f"python benchmark_scripts/measure_env_size.py --report-path {report_path}",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "reported_python_path": reported_python_path,
                "observed": observed,
                "meta": {"python": reported_python_path, "git_commit": git_commit(root), "timestamp_utc": utc_timestamp()},
                "failure_category": "env_size_failed",
                "error_excerpt": msg,
            }
            write_json(results_path, payload)
            return 1

        probe, raw = python_probe_env(reported_python_path, timeout_sec=60)
        logf.write("[env_size] probe_output_begin\n")
        logf.write(raw + ("\n" if not raw.endswith("\n") else ""))
        logf.write("[env_size] probe_output_end\n")

        if not probe:
            msg = "failed to probe sys.prefix/site-packages via reported python"
            logf.write(f"[env_size] {msg}\n")
            payload = {
                "status": "failure",
                "skip_reason": "not_applicable",
                "exit_code": 1,
                "stage": "env_size",
                "task": "measure",
                "command": f"{reported_python_path} -c <env_probe>",
                "timeout_sec": 120,
                "framework": "unknown",
                "assets": {
                    "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                    "model": {"path": "", "source": "", "version": "", "sha256": ""},
                },
                "reported_python_path": reported_python_path,
                "observed": observed,
                "meta": {"python": reported_python_path, "git_commit": git_commit(root), "timestamp_utc": utc_timestamp()},
                "failure_category": "env_size_failed",
                "error_excerpt": msg,
            }
            write_json(results_path, payload)
            return 1

        env_prefix = Path(str(probe.get("sys_prefix", "") or ""))
        site_pkgs = [str(p) for p in (probe.get("site_packages") or []) if isinstance(p, str)]
        user_site = str(probe.get("user_site", "") or "")
        if user_site:
            site_pkgs.append(user_site)

        observed["env_prefix"] = str(env_prefix)

        t0 = time.time()
        prefix_size, warnings = recursive_size_bytes(env_prefix) if env_prefix.exists() else (0, [])
        observed["warnings"].extend(warnings)
        observed["env_prefix_size_MB"] = int(round(prefix_size / (1024 * 1024)))
        logf.write(f"[env_size] env_prefix_size_bytes={prefix_size} elapsed_sec={time.time()-t0:.2f}\n")

        site_entries: List[Dict[str, Any]] = []
        total_site = 0
        for sp in site_pkgs:
            sp_path = Path(sp)
            if not sp_path.exists():
                continue
            sz, warn = recursive_size_bytes(sp_path)
            total_site += sz
            observed["warnings"].extend(warn)
            site_entries.append({"path": str(sp_path), "size_bytes": int(sz)})
            logf.write(f"[env_size] site_packages_size_bytes path={sp_path} size={sz}\n")

        observed["site_packages"] = site_entries
        observed["site_packages_total_bytes"] = int(total_site)

        status = "success"
        exit_code = 0
        failure_category = ""

    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-240:]
        error_excerpt = "\n".join(tail).strip()
    except Exception:
        error_excerpt = ""

    payload = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"{reported_python_path} -c <env_probe> ; size_walk",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "python": reported_python_path,
            "git_commit": git_commit(root),
            "timestamp_utc": utc_timestamp(),
            "env_size_definition": "A) sys.prefix only (plus site-packages breakdown)",
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }
    write_json(results_path, payload)
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
