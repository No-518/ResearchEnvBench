#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _tail_lines(path: Path, *, max_lines: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def _safe_json_load(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError as e:
        return None, f"invalid_json: {e}"
    except Exception as e:
        return None, f"read_error: {e}"


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        ).strip()
        return out
    except Exception:
        return ""


def _is_executable_file(path: str) -> bool:
    try:
        p = Path(path)
        return p.exists() and p.is_file() and os.access(str(p), os.X_OK)
    except Exception:
        return False


def _resolve_report_path(cli_report_path: Optional[str]) -> str:
    return cli_report_path or os.environ.get("SCIMLOPSBENCH_REPORT") or DEFAULT_REPORT_PATH


ENV_INFO_CODE = r"""
import json
import site
import sys

payload = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_packages": [],
  "user_site": "",
}

try:
  payload["site_packages"] = list(site.getsitepackages())
except Exception:
  payload["site_packages"] = []

try:
  payload["user_site"] = site.getusersitepackages()
except Exception:
  payload["user_site"] = ""

print(json.dumps(payload, ensure_ascii=False))
""".strip()


def _dir_size_bytes(root: Path, warnings: List[str]) -> int:
    total = 0
    stack = [root]
    while stack:
        p = stack.pop()
        try:
            st = p.lstat()
        except PermissionError as e:
            warnings.append(f"permission_error: {p}: {e}")
            continue
        except FileNotFoundError:
            continue
        except Exception as e:
            warnings.append(f"stat_error: {p}: {e}")
            continue

        if stat.S_ISLNK(st.st_mode):
            continue
        if stat.S_ISREG(st.st_mode):
            total += int(st.st_size)
            continue
        if stat.S_ISDIR(st.st_mode):
            try:
                with os.scandir(p) as it:
                    for entry in it:
                        stack.append(Path(entry.path))
            except PermissionError as e:
                warnings.append(f"permission_error: {p}: {e}")
            except FileNotFoundError:
                continue
            except Exception as e:
                warnings.append(f"scandir_error: {p}: {e}")
            continue
    return total


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Measure environment size from report.json python_path.")
    parser.add_argument(
        "--report-path",
        default=None,
        help="Override report.json path (highest priority).",
    )
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    stage_dir = repo_root / "build_output" / "env_size"
    _ensure_dir(stage_dir)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    error_message = ""

    report_path = _resolve_report_path(args.report_path)
    reported_python_path = ""

    observed: Dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
    }
    warnings: List[str] = []

    command_str = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).name))} --report-path {shlex.quote(report_path)}"

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[env_size] report_path={report_path}\n")
        log_f.write(f"[env_size] timestamp_utc={_utc_timestamp()}\n")

        report_file = Path(report_path)
        report_json, report_err = _safe_json_load(report_file)
        if report_err is not None:
            error_message = f"report.json error at {report_path}: {report_err}"
            log_f.write(f"[env_size] {error_message}\n")
        else:
            reported_python_path = str((report_json or {}).get("python_path") or "")
            log_f.write(f"[env_size] reported_python_path={reported_python_path}\n")

            if not reported_python_path or not _is_executable_file(reported_python_path):
                error_message = f"python_path is missing or not executable: {reported_python_path}"
                log_f.write(f"[env_size] {error_message}\n")
            else:
                probe_cmd = [reported_python_path, "-c", ENV_INFO_CODE]
                log_f.write(f"[env_size] probe_cmd={' '.join(shlex.quote(t) for t in probe_cmd)}\n")
                log_f.flush()
                try:
                    proc = subprocess.run(
                        probe_cmd,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=30,
                    )
                    if proc.stderr:
                        log_f.write("[env_size] probe_stderr:\n")
                        log_f.write(proc.stderr)
                        if not proc.stderr.endswith("\n"):
                            log_f.write("\n")
                    if proc.returncode != 0:
                        error_message = f"python probe failed with exit_code={proc.returncode}"
                        log_f.write(f"[env_size] {error_message}\n")
                    else:
                        env_info = json.loads((proc.stdout or "").strip() or "{}")
                        env_prefix = str(env_info.get("sys_prefix") or "")
                        observed["env_prefix"] = env_prefix
                        sp_paths: List[str] = []
                        for p in env_info.get("site_packages") or []:
                            if isinstance(p, str) and p:
                                sp_paths.append(p)
                        user_site = env_info.get("user_site")
                        if isinstance(user_site, str) and user_site:
                            sp_paths.append(user_site)
                        sp_paths = sorted({p for p in sp_paths})

                        prefix_size_bytes = 0
                        if env_prefix:
                            prefix_size_bytes = _dir_size_bytes(Path(env_prefix), warnings)
                        observed["env_prefix_size_MB"] = int(round(prefix_size_bytes / (1024 * 1024)))

                        sp_total = 0
                        sp_entries = []
                        for p in sp_paths:
                            size_bytes = 0
                            if Path(p).exists():
                                size_bytes = _dir_size_bytes(Path(p), warnings)
                            sp_entries.append({"path": p, "size_bytes": int(size_bytes)})
                            sp_total += int(size_bytes)
                        observed["site_packages"] = sp_entries
                        observed["site_packages_total_bytes"] = int(sp_total)

                        status = "success"
                        exit_code = 0
                        failure_category = "unknown"
                except Exception as e:
                    error_message = f"env size probe failed: {e}"
                    log_f.write(f"[env_size] {error_message}\n")

        if warnings:
            log_f.write("\n[env_size] warnings:\n")
            for w in warnings[:200]:
                log_f.write(w + "\n")

    payload: Dict[str, Any] = {
        "status": status,
        "exit_code": int(exit_code),
        "stage": "env_size",
        "task": "measure",
        "command": command_str,
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "git_commit": _git_commit(repo_root),
            "report_path": report_path,
            "timestamp_utc": _utc_timestamp(),
            "warnings_count": len(warnings),
            "warnings_preview": warnings[:20],
        },
        "failure_category": failure_category if status == "failure" else "unknown",
        "error_excerpt": _tail_lines(log_path) if status == "failure" else "",
    }
    if status == "failure":
        payload["meta"]["error"] = error_message

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())

