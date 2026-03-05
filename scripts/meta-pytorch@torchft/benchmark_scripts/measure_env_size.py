#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _report_path(cli_report_path: str | None) -> Path:
    if cli_report_path and cli_report_path.strip():
        return Path(cli_report_path.strip())
    env_report = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_report and env_report.strip():
        return Path(env_report.strip())
    return Path("/opt/scimlopsbench/report.json")


def _tail(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return "\n".join(txt.splitlines()[-max_lines:]).strip()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git_commit(repo_root: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:
        return ""


def _iter_paths_unique(paths: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        p = str(p)
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _dir_size_bytes(root: Path, warnings: list[str]) -> int:
    total = 0
    stack = [root]
    while stack:
        p = stack.pop()
        try:
            if p.is_symlink():
                try:
                    total += p.lstat().st_size
                except Exception:
                    pass
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
                except PermissionError:
                    warnings.append(f"permission_denied:{p}")
                except FileNotFoundError:
                    continue
                except Exception as e:
                    warnings.append(f"scandir_failed:{p}:{e}")
        except Exception:
            continue
    return total


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _report_path(args.report_path)

    assets = {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }
    manifest = repo_root / "benchmark_assets" / "manifest.json"
    if manifest.exists():
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
            a = m.get("assets", m)
            assets = {
                "dataset": a.get("dataset", assets["dataset"]),
                "model": a.get("model", assets["model"]),
            }
        except Exception:
            pass

    with log_path.open("w", encoding="utf-8") as log_fp:
        log_fp.write(f"[env_size] utc_start={_utc_timestamp()}\n")
        log_fp.write(f"[env_size] report_path={report_path}\n")

        status = "failure"
        failure_category = "env_size_failed"
        python_path = ""

        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            python_path = str(report.get("python_path", "")).strip()
        except Exception as e:
            log_fp.write(f"[env_size] error: failed to read/parse report: {e}\n")
        else:
            if not python_path:
                log_fp.write("[env_size] error: report missing python_path\n")
            else:
                snippet = r"""
import json, site, sys
payload = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_packages": site.getsitepackages() if hasattr(site, "getsitepackages") else [],
  "user_site": site.getusersitepackages() if hasattr(site, "getusersitepackages") else "",
}
print(json.dumps(payload))
"""
                cp = subprocess.run(
                    [python_path, "-c", snippet],
                    cwd=repo_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                log_fp.write(cp.stdout)
                if cp.stderr:
                    log_fp.write(cp.stderr)
                if cp.returncode != 0:
                    log_fp.write(f"[env_size] error: python snippet exit={cp.returncode}\n")
                else:
                    try:
                        info = json.loads(cp.stdout.strip().splitlines()[-1])
                    except Exception as e:
                        log_fp.write(f"[env_size] error: failed to parse snippet output: {e}\n")
                    else:
                        env_prefix = Path(str(info.get("sys_prefix", ""))).resolve()
                        site_pkgs = _iter_paths_unique(
                            [*info.get("site_packages", []), info.get("user_site", "")]
                        )

                        warnings: list[str] = []
                        env_prefix_size = _dir_size_bytes(env_prefix, warnings) if env_prefix.exists() else 0

                        site_entries = []
                        site_total = 0
                        for sp in site_pkgs:
                            sp_path = Path(sp)
                            sp_size = _dir_size_bytes(sp_path, warnings) if sp_path.exists() else 0
                            site_entries.append({"path": str(sp_path), "size_bytes": sp_size})
                            site_total += sp_size

                        observed = {
                            "env_prefix": str(env_prefix),
                            "env_prefix_size_MB": int(env_prefix_size / (1024 * 1024)),
                            "site_packages": site_entries,
                            "site_packages_total_bytes": site_total,
                            "warnings": warnings,
                        }

                        payload: dict[str, Any] = {
                            "status": "success",
                            "skip_reason": "not_applicable",
                            "exit_code": 0,
                            "stage": "env_size",
                            "task": "measure",
                            "command": f"python {Path(__file__).name}",
                            "timeout_sec": 120,
                            "framework": "unknown",
                            "assets": assets,
                            "reported_python_path": python_path,
                            "observed": observed,
                            "meta": {
                                "python": sys.executable,
                                "timestamp_utc": _utc_timestamp(),
                                "git_commit": _git_commit(repo_root),
                                "env_vars": {k: os.environ.get(k, "") for k in sorted(os.environ) if k.startswith("SCIMLOPSBENCH_")},
                                "report_path": str(report_path),
                            },
                            "failure_category": "not_applicable",
                            "error_excerpt": "",
                        }

                        _write_json(results_path, payload)
                        log_fp.write("[env_size] success\n")
                        return 0

    # Failure path (report missing/invalid, python failed, etc.)
    payload = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": f"python {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": assets,
        "reported_python_path": python_path,
        "observed": {},
        "meta": {
            "python": sys.executable,
            "timestamp_utc": _utc_timestamp(),
            "git_commit": _git_commit(repo_root),
            "env_vars": {k: os.environ.get(k, "") for k in sorted(os.environ) if k.startswith("SCIMLOPSBENCH_")},
            "report_path": str(report_path),
        },
        "failure_category": failure_category,
        "error_excerpt": _tail(log_path),
    }
    _write_json(results_path, payload)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
