#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: pathlib.Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tail_text(path: pathlib.Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)


def resolve_report_path(cli_path: str | None) -> pathlib.Path:
    if cli_path:
        return pathlib.Path(cli_path)
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        return pathlib.Path(os.environ["SCIMLOPSBENCH_REPORT"])
    return pathlib.Path(DEFAULT_REPORT_PATH)


def dir_size_bytes(path: pathlib.Path, warnings: list[str]) -> int:
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size
    except Exception as e:
        warnings.append(f"stat_failed:{path}:{type(e).__name__}:{e}")
        return 0

    stack = [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            try:
                                total += entry.stat(follow_symlinks=False).st_size
                            except Exception as e:
                                warnings.append(f"stat_failed:{entry.path}:{type(e).__name__}:{e}")
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(pathlib.Path(entry.path))
                    except PermissionError as e:
                        warnings.append(f"perm_denied:{entry.path}:{e}")
        except PermissionError as e:
            warnings.append(f"perm_denied:{p}:{e}")
        except FileNotFoundError:
            continue
        except Exception as e:
            warnings.append(f"scan_failed:{p}:{type(e).__name__}:{e}")
    return total


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Measure Python environment size from report.json python_path.")
    ap.add_argument("--report-path", default=None, help="Override report.json path.")
    args = ap.parse_args(argv)

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    out_dir = repo_root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = resolve_report_path(args.report_path)

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    reported_python_path = ""
    observed: dict[str, Any] = {}
    warnings: list[str] = []

    with log_path.open("w", encoding="utf-8") as log_fp:
        log_fp.write(f"[env_size] time_utc={now_utc_iso()}\n")
        log_fp.write(f"[env_size] report_path={report_path}\n")

        report: dict[str, Any] | None = None
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log_fp.write("[env_size] report missing\n")
        except Exception as e:
            log_fp.write(f"[env_size] report parse failed: {type(e).__name__}: {e}\n")

        if isinstance(report, dict):
            python_path_val = report.get("python_path")
            if isinstance(python_path_val, str):
                reported_python_path = python_path_val

        if not reported_python_path:
            log_fp.write("[env_size] python_path missing in report\n")
        elif not (os.path.isfile(reported_python_path) and os.access(reported_python_path, os.X_OK)):
            log_fp.write(f"[env_size] python_path not executable: {reported_python_path!r}\n")
        else:
            info_code = r"""
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
  out["user_site"] = site.getusersitepackages()
except Exception:
  out["user_site"] = ""
print(json.dumps(out))
"""
            cmd = [reported_python_path, "-c", info_code]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=repo_root)
            except subprocess.TimeoutExpired:
                log_fp.write("[env_size] python info probe timeout\n")
            else:
                if proc.stderr:
                    log_fp.write(proc.stderr + ("\n" if not proc.stderr.endswith("\n") else ""))
                try:
                    info = json.loads(proc.stdout.strip() or "{}")
                except Exception as e:
                    log_fp.write(f"[env_size] failed to parse probe JSON: {type(e).__name__}: {e}\n")
                    info = {}

                env_prefix_str = str(info.get("sys_prefix", "") or "")
                env_prefix = pathlib.Path(env_prefix_str) if env_prefix_str else None
                site_paths: list[pathlib.Path] = []
                for p in info.get("site_packages", []) or []:
                    if isinstance(p, str) and p:
                        site_paths.append(pathlib.Path(p))
                user_site = info.get("user_site")
                if isinstance(user_site, str) and user_site:
                    site_paths.append(pathlib.Path(user_site))

                env_prefix_size = dir_size_bytes(env_prefix, warnings) if env_prefix is not None else 0
                site_items: list[dict[str, Any]] = []
                site_total = 0
                for sp in site_paths:
                    size = dir_size_bytes(sp, warnings)
                    site_items.append({"path": str(sp), "size_bytes": size})
                    site_total += size

                observed = {
                    "env_prefix": env_prefix_str,
                    "env_prefix_size_MB": int(round(env_prefix_size / (1024 * 1024))),
                    "site_packages": site_items,
                    "site_packages_total_bytes": site_total,
                    "warnings": warnings,
                }
                status = "success"
                exit_code = 0
                failure_category = ""

    result = {
        "status": status,
        "skip_reason": "not_applicable" if status == "success" else "unknown",
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"{pathlib.Path(__file__).name} --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {"timestamp_utc": now_utc_iso()},
        "failure_category": failure_category,
        "error_excerpt": tail_text(log_path),
    }
    write_json(results_path, result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
