#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_now() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).replace(microsecond=0).isoformat()


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return (cp.stdout or "").strip()
    except Exception:
        return ""


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return Path(DEFAULT_REPORT_PATH)


def _read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception as e:
        return None, f"read_error:{type(e).__name__}:{e}"
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except Exception as e:
        return None, f"invalid_json:{type(e).__name__}:{e}"


def _probe_env(python_exe: str) -> Tuple[Optional[dict], Optional[str]]:
    code = (
        "import json, sys, site\n"
        "payload={\n"
        " 'sys_executable': sys.executable,\n"
        " 'sys_prefix': sys.prefix,\n"
        " 'site_packages': list(dict.fromkeys([p for p in (site.getsitepackages() if hasattr(site,'getsitepackages') else []) if isinstance(p,str)])),\n"
        " 'user_site': (site.getusersitepackages() if hasattr(site,'getusersitepackages') else None),\n"
        "}\n"
        "print(json.dumps(payload))\n"
    )
    try:
        cp = subprocess.run(
            [python_exe, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=60,
        )
        if cp.returncode != 0:
            return None, (cp.stderr or cp.stdout).strip()[-4000:]
        return json.loads(cp.stdout), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except PermissionError:
                        warnings.append(f"permission_error:{entry.path}")
                    except FileNotFoundError:
                        continue
                    except Exception as e:
                        warnings.append(f"stat_error:{entry.path}:{type(e).__name__}:{e}")
        except PermissionError:
            warnings.append(f"permission_error:{p}")
        except FileNotFoundError:
            continue
        except Exception as e:
            warnings.append(f"scan_error:{p}:{type(e).__name__}:{e}")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure environment size from agent report python_path.")
    ap.add_argument("--report-path", default=None, help="Override report path (else SCIMLOPSBENCH_REPORT or default).")
    args = ap.parse_args()

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "env_size"
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)
    command = f"{sys.executable} {Path(__file__).as_posix()} --report-path {report_path}"

    base_observed: Dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
    }
    warnings: List[str] = []

    report, err = _read_json(report_path)
    python_path = (report or {}).get("python_path") if isinstance(report, dict) else None

    if err is not None or not isinstance(python_path, str) or not python_path:
        msg = f"report_missing_or_invalid: {err or 'missing python_path'}"
        _write_text(log_path, msg + "\n")
        _write_json(
            results_path,
            {
                "status": "failure",
                "exit_code": 1,
                "stage": "env_size",
                "task": "measure",
                "command": command,
                "reported_python_path": str(python_path or ""),
                "observed": base_observed,
                "meta": {
                    "git_commit": _git_commit(repo_root),
                    "timestamp_utc": _utc_now(),
                    "warnings": warnings,
                    "report_path": str(report_path),
                },
                "failure_category": "env_size_failed",
                "error_excerpt": msg,
            },
        )
        return 1

    p = Path(python_path)
    if not (p.exists() and os.access(p, os.X_OK)):
        msg = f"python_path_invalid_or_not_executable: {python_path}"
        _write_text(log_path, msg + "\n")
        _write_json(
            results_path,
            {
                "status": "failure",
                "exit_code": 1,
                "stage": "env_size",
                "task": "measure",
                "command": command,
                "reported_python_path": python_path,
                "observed": base_observed,
                "meta": {
                    "git_commit": _git_commit(repo_root),
                    "timestamp_utc": _utc_now(),
                    "warnings": warnings,
                    "report_path": str(report_path),
                },
                "failure_category": "env_size_failed",
                "error_excerpt": msg,
            },
        )
        return 1

    probe, probe_err = _probe_env(python_path)
    if probe_err is not None or not isinstance(probe, dict):
        msg = f"python_probe_failed: {probe_err or 'unknown'}"
        _write_text(log_path, msg + "\n")
        _write_json(
            results_path,
            {
                "status": "failure",
                "exit_code": 1,
                "stage": "env_size",
                "task": "measure",
                "command": command,
                "reported_python_path": python_path,
                "observed": base_observed,
                "meta": {
                    "git_commit": _git_commit(repo_root),
                    "timestamp_utc": _utc_now(),
                    "warnings": warnings,
                    "report_path": str(report_path),
                },
                "failure_category": "env_size_failed",
                "error_excerpt": msg,
            },
        )
        return 1

    env_prefix = str(probe.get("sys_prefix") or "")
    site_pkgs: List[str] = []
    if isinstance(probe.get("site_packages"), list):
        site_pkgs.extend([s for s in probe["site_packages"] if isinstance(s, str)])
    user_site = probe.get("user_site")
    if isinstance(user_site, str) and user_site:
        site_pkgs.append(user_site)
    site_pkgs = list(dict.fromkeys(site_pkgs))

    observed = dict(base_observed)
    observed["env_prefix"] = env_prefix

    if env_prefix and Path(env_prefix).exists():
        env_size = _dir_size_bytes(Path(env_prefix), warnings)
        observed["env_prefix_size_MB"] = int(round(env_size / (1024 * 1024)))
    else:
        warnings.append(f"env_prefix_missing:{env_prefix}")

    sp_total = 0
    sp_entries: List[Dict[str, Any]] = []
    for sp in site_pkgs:
        sp_path = Path(sp)
        if not sp_path.exists():
            warnings.append(f"site_packages_missing:{sp}")
            continue
        size = _dir_size_bytes(sp_path, warnings)
        sp_entries.append({"path": sp, "size_bytes": size})
        sp_total += size
    observed["site_packages"] = sp_entries
    observed["site_packages_total_bytes"] = sp_total

    log_lines = [
        f"[env_size] reported_python_path={python_path}",
        f"[env_size] sys.prefix={env_prefix}",
        f"[env_size] env_prefix_size_MB={observed['env_prefix_size_MB']}",
        f"[env_size] site_packages_total_bytes={sp_total}",
        f"[env_size] warnings_count={len(warnings)}",
    ]
    _write_text(log_path, "\n".join(log_lines) + "\n")

    _write_json(
        results_path,
        {
            "status": "success",
            "exit_code": 0,
            "stage": "env_size",
            "task": "measure",
            "command": command,
            "reported_python_path": python_path,
            "observed": observed,
            "meta": {
                "git_commit": _git_commit(repo_root),
                "timestamp_utc": _utc_now(),
                "warnings": warnings,
                "report_path": str(report_path),
            },
            "failure_category": "unknown",
            "error_excerpt": "",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

