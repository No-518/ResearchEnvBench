#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return Path("/opt/scimlopsbench/report.json")


def _tail_text(path: Path, max_lines: int = 220) -> str:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    return "\n".join(lines[-max_lines:])


def _git_commit(repo: Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            check=False,
            capture_output=True,
            text=True,
        )
        if cp.returncode == 0:
            return cp.stdout.strip()
    except Exception:
        pass
    return ""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


def _dir_size_bytes(path: Path, warnings: list[str]) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except PermissionError as e:
                warnings.append(f"PermissionError: {p}: {e}")
            except FileNotFoundError:
                continue
    except PermissionError as e:
        warnings.append(f"PermissionError walking {path}: {e}")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure environment disk size")
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    repo = _repo_root()
    out_dir = repo / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)
    cmd_display = f"python benchmark_scripts/measure_env_size.py --report-path {report_path}"

    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"timestamp_utc={_utc_timestamp()}\n")
        logf.write(f"report_path={report_path}\n")

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    observed: dict[str, Any] = {}
    error_excerpt = ""

    if not report_path.exists():
        error_excerpt = f"Report not found: {report_path}"
    else:
        try:
            report = _read_json(report_path)
            python_path = report.get("python_path")
            if not isinstance(python_path, str) or not python_path.strip():
                raise RuntimeError("python_path missing in report.json")
            python_path = python_path.strip()
            observed["reported_python_path"] = python_path
            if not _is_executable_file(Path(python_path)):
                raise RuntimeError(f"python_path not executable: {python_path!r}")

            code = r"""
import json, site, sys
payload = {
  "sys_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_packages": site.getsitepackages() if hasattr(site, "getsitepackages") else [],
  "user_site": site.getusersitepackages() if hasattr(site, "getusersitepackages") else None,
}
print(json.dumps(payload))
"""
            cp = subprocess.run(
                [python_path, "-c", code],
                check=False,
                capture_output=True,
                text=True,
            )
            if cp.returncode != 0:
                raise RuntimeError(f"python probe failed: rc={cp.returncode}\n{cp.stderr}")
            probe = json.loads(cp.stdout.strip() or "{}")
            env_prefix = Path(probe.get("sys_prefix", ""))
            site_packages = []
            for p in probe.get("site_packages", []) or []:
                if isinstance(p, str) and p:
                    site_packages.append(Path(p))
            user_site = probe.get("user_site")
            if isinstance(user_site, str) and user_site:
                site_packages.append(Path(user_site))

            warnings: list[str] = []
            env_prefix_size = _dir_size_bytes(env_prefix, warnings) if env_prefix else 0
            sp_sizes = []
            sp_total = 0
            for sp in site_packages:
                size = _dir_size_bytes(sp, warnings) if sp.exists() else 0
                sp_sizes.append({"path": str(sp), "size_bytes": int(size)})
                sp_total += int(size)

            observed.update(
                {
                    "env_prefix": str(env_prefix),
                    "env_prefix_size_MB": int(round(env_prefix_size / (1024 * 1024))),
                    "site_packages": sp_sizes,
                    "site_packages_total_bytes": int(sp_total),
                    "warnings": warnings,
                }
            )
            status = "success"
            exit_code = 0
            failure_category = "unknown"
        except Exception as e:
            error_excerpt = repr(e)

    payload = {
        "status": status,
        "skip_reason": "unknown",
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": cmd_display,
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
            "model": {"path": "", "source": "unknown", "version": "unknown", "sha256": "unknown"},
        },
        "reported_python_path": observed.get("reported_python_path", ""),
        "observed": observed,
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo),
            "timestamp_utc": _utc_timestamp(),
            "report_path": str(report_path),
            "env_vars": {
                k: ("***REDACTED***" if k in {"HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "OPENAI_API_KEY"} else v)
                for k, v in os.environ.items()
                if k
                in {
                    "CUDA_VISIBLE_DEVICES",
                    "SCIMLOPSBENCH_REPORT",
                    "SCIMLOPSBENCH_PYTHON",
                    "HF_TOKEN",
                    "HUGGINGFACE_HUB_TOKEN",
                    "OPENAI_API_KEY",
                }
            },
            "decision_reason": "Measure disk usage of the benchmark python environment (sys.prefix and site-packages paths) using python_path from report.json.",
        },
        "failure_category": failure_category,
        "error_excerpt": error_excerpt or _tail_text(log_path),
    }

    with log_path.open("a", encoding="utf-8") as logf:
        logf.write("\n--- observed ---\n")
        logf.write(json.dumps(observed, ensure_ascii=False, indent=2) + "\n")

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
