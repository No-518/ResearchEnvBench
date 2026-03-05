#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"
DEFAULT_TIMEOUT_SEC = 120
SAFETY_MARGIN_SEC = 5


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def safe_env_snapshot() -> dict[str, str]:
    keys = [
        "SCIMLOPSBENCH_REPORT",
        "SCIMLOPSBENCH_PYTHON",
        "PATH",
        "PYTHONPATH",
        "XDG_CACHE_HOME",
    ]
    return {k: os.environ.get(k, "") for k in keys if k in os.environ}


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing_file: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid_json: {path}: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"read_error: {path}: {e}"


def is_executable(path: str) -> bool:
    try:
        p = Path(path)
        return p.exists() and p.is_file() and os.access(str(p), os.X_OK)
    except Exception:  # noqa: BLE001
        return False


def git_commit(root: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


PROBE_ENV_CODE = r"""
import json
import site
import sys

out = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_getsitepackages": [],
  "site_getusersitepackages": None,
}

try:
    out["site_getsitepackages"] = list(site.getsitepackages())
except Exception:
    out["site_getsitepackages"] = []

try:
    out["site_getusersitepackages"] = site.getusersitepackages()
except Exception:
    out["site_getusersitepackages"] = None

print(json.dumps(out))
"""


class DeadlineExceeded(Exception):
    pass


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _du_size_bytes(path: Path, *, deadline: float, warnings: list[str]) -> int | None:
    du = shutil.which("du")
    if not du:
        return None
    # Ensure we keep a small margin to write logs/results.
    timeout_sec = _remaining(deadline) - SAFETY_MARGIN_SEC
    if timeout_sec <= 0:
        raise DeadlineExceeded("env_size deadline exceeded before du run")
    cmd = [du, "-sb", str(path)]
    try:
        res = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        if res.returncode != 0:
            warnings.append(f"du_failed: {shlex.join(cmd)} rc={res.returncode} stderr={(res.stderr or '').strip()}")
            return None
        # Format: "<bytes>\t<path>"
        first = (res.stdout or "").strip().splitlines()[0] if (res.stdout or "").strip() else ""
        parts = first.split()
        if not parts:
            return None
        return int(parts[0])
    except subprocess.TimeoutExpired as e:
        warnings.append(f"du_timeout: {shlex.join(cmd)} timeout_sec={timeout_sec:.1f}")
        raise DeadlineExceeded(str(e)) from e
    except Exception as e:  # noqa: BLE001
        warnings.append(f"du_error: {shlex.join(cmd)} error={e}")
        return None


def dir_size_bytes(path: Path, warnings: list[str], *, deadline: float) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            if time.monotonic() > deadline - SAFETY_MARGIN_SEC:
                raise DeadlineExceeded(f"deadline exceeded while scanning: {path}")
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += dir_size_bytes(Path(entry.path), warnings, deadline=deadline)
            except PermissionError as e:
                warnings.append(f"permission_error: {entry.path}: {e}")
            except FileNotFoundError:
                continue
            except OSError as e:
                warnings.append(f"os_error: {entry.path}: {e}")
    except PermissionError as e:
        warnings.append(f"permission_error: {path}: {e}")
    except FileNotFoundError:
        return 0
    except OSError as e:
        warnings.append(f"os_error: {path}: {e}")
    return total


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Measure environment size for the reported python environment.")
    p.add_argument("--report-path", default=None, help="Override report.json path")
    p.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"Overall time budget in seconds (default: {DEFAULT_TIMEOUT_SEC}).",
    )
    args = p.parse_args(argv)

    start = time.monotonic()
    deadline = start + max(1, int(args.timeout_sec))

    root = repo_root()
    stage_dir = root / "build_output" / "env_size"
    stage_dir.mkdir(parents=True, exist_ok=True)
    log_path = stage_dir / "log.txt"
    results_path = stage_dir / "results.json"

    def log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        print(msg)

    report_path = resolve_report_path(args.report_path)
    report, report_err = load_json(report_path)

    reported_python_path = ""
    if isinstance(report, dict) and isinstance(report.get("python_path"), str):
        reported_python_path = str(report.get("python_path", "")).strip()

    failure_category = "unknown"
    exit_code = 1
    status = "failure"
    warnings: list[str] = []
    observed: dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
    }

    if report_err is not None:
        log(f"Report error: {report_err}")
        failure_category = "env_size_failed"
    elif not reported_python_path:
        log("report.json missing python_path.")
        failure_category = "env_size_failed"
    elif not is_executable(reported_python_path):
        log(f"python_path is not an executable file: {reported_python_path}")
        failure_category = "env_size_failed"
    else:
        cmd = [reported_python_path, "-c", PROBE_ENV_CODE]
        log(f"[{now_utc_iso()}] Probing environment paths: {shlex.join(cmd)}")
        try:
            res = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=min(30, max(1, int(_remaining(deadline) - SAFETY_MARGIN_SEC))),
                cwd=str(root),
                env=os.environ.copy(),
            )
            if res.stderr:
                log("[stderr]\n" + res.stderr.strip())
            probe = json.loads((res.stdout or "").strip() or "{}")
            env_prefix = str(probe.get("sys_prefix", "")).strip()
            site_pkgs: list[str] = []
            if isinstance(probe.get("site_getsitepackages"), list):
                site_pkgs.extend([str(p) for p in probe.get("site_getsitepackages") if str(p).strip()])
            user_site = probe.get("site_getusersitepackages")
            if isinstance(user_site, str) and user_site.strip():
                site_pkgs.append(user_site.strip())
            # De-dup while preserving order.
            seen: set[str] = set()
            site_pkgs = [p for p in site_pkgs if not (p in seen or seen.add(p))]

            size_warnings: list[str] = []
            partial = False
            env_prefix_bytes = 0
            try:
                if env_prefix:
                    du_bytes = _du_size_bytes(Path(env_prefix), deadline=deadline, warnings=size_warnings)
                    if du_bytes is not None:
                        env_prefix_bytes = du_bytes
                    else:
                        env_prefix_bytes = dir_size_bytes(Path(env_prefix), size_warnings, deadline=deadline)
            except DeadlineExceeded as e:
                partial = True
                size_warnings.append(f"deadline_exceeded: env_prefix: {e}")
            site_entries: list[dict[str, Any]] = []
            site_total = 0
            for sp in site_pkgs:
                if partial:
                    size_warnings.append("deadline_exceeded: skipping remaining site-packages measurements")
                    break
                if _remaining(deadline) <= SAFETY_MARGIN_SEC:
                    partial = True
                    size_warnings.append("deadline_exceeded: before site-packages measurements")
                    break
                pth = Path(sp)
                if not pth.exists():
                    continue
                try:
                    du_bytes = _du_size_bytes(pth, deadline=deadline, warnings=size_warnings)
                    b = du_bytes if du_bytes is not None else dir_size_bytes(pth, size_warnings, deadline=deadline)
                    site_entries.append({"path": str(pth), "size_bytes": b})
                    site_total += b
                except DeadlineExceeded as e:
                    partial = True
                    size_warnings.append(f"deadline_exceeded: site_packages: {pth}: {e}")
                    break

            warnings.extend(size_warnings)
            observed = {
                "env_prefix": env_prefix,
                "env_prefix_size_MB": int(env_prefix_bytes / 1_048_576),
                "site_packages": site_entries,
                "site_packages_total_bytes": site_total,
            }

            if partial:
                status = "failure"
                exit_code = 1
                failure_category = "env_size_failed"
            else:
                status = "success"
                exit_code = 0
                failure_category = "unknown"
        except subprocess.TimeoutExpired:
            log("Timed out probing environment paths.")
            failure_category = "env_size_failed"
        except json.JSONDecodeError as e:
            log(f"Failed to parse probe JSON: {e}")
            failure_category = "invalid_json"
        except Exception as e:  # noqa: BLE001
            log(f"Unexpected env size failure: {e}")
            failure_category = "env_size_failed"

    # Error excerpt.
    error_excerpt = ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        error_excerpt = "\n".join(lines[-220:])[-8000:]
    except Exception:  # noqa: BLE001
        pass

    cmd_str = f"python {Path(__file__).name} --report-path {report_path} --timeout-sec {args.timeout_sec}"

    results: dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": cmd_str,
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "git_commit": git_commit(root),
            "timestamp_utc": now_utc_iso(),
            "env_vars": safe_env_snapshot(),
            "warnings": warnings,
            "report_path": str(report_path),
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt,
    }

    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
