#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_last_lines(path: Path, max_lines: int = 220) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= max_lines:
                step = block if size >= block else size
                size -= step
                f.seek(size, os.SEEK_SET)
                data = f.read(step) + data
            lines = data.splitlines()[-max_lines:]
            return "\n".join(l.decode("utf-8", errors="replace") for l in lines)
    except Exception:
        return ""


def _resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT", "")
    if env_path:
        return Path(env_path)
    return Path(DEFAULT_REPORT_PATH)


def _du_bytes(root: Path, warnings: list[str]) -> int:
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    total += p.stat().st_size
                except PermissionError:
                    warnings.append(f"permission_denied: {p}")
                except FileNotFoundError:
                    continue
                except Exception as e:
                    warnings.append(f"stat_failed: {p}: {e!r}")
    except PermissionError:
        warnings.append(f"permission_denied_walk: {root}")
    except Exception as e:
        warnings.append(f"walk_failed: {root}: {e!r}")
    return total


def _base_assets() -> dict[str, Any]:
    return {
        "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
        "model": {"path": "", "source": "", "version": "", "sha256": ""},
    }


def _git_commit(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Measure environment size for the benchmarked python.")
    parser.add_argument("--report-path", default=None)
    ns = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "env_size"
    _ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stage = "env_size"
    task = "measure"
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"

    status = "failure"
    exit_code = 1
    failure_category = "env_size_failed"
    report_path = _resolve_report_path(ns.report_path)
    reported_python_path = ""

    observed: dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
    }
    warnings: list[str] = []

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[env_size] report_path={report_path}\n")
        try:
            report = _read_json(report_path)
        except FileNotFoundError:
            log_f.write("[env_size] missing report.json\n")
            report = {}
        except json.JSONDecodeError as e:
            log_f.write(f"[env_size] invalid report.json: {e}\n")
            report = {}

        reported_python_path = str(report.get("python_path") or "")
        if not reported_python_path:
            log_f.write("[env_size] report missing python_path\n")
        elif not (Path(reported_python_path).exists() and os.access(reported_python_path, os.X_OK)):
            log_f.write(f"[env_size] python_path not executable: {reported_python_path}\n")
            reported_python_path = ""

        if not reported_python_path:
            status = "failure"
            exit_code = 1
            failure_category = "env_size_failed"
        else:
            probe_code = r"""
import json
import site
import sys
out = {
  "sys_prefix": sys.prefix,
  "site_packages": [],
  "user_site": None,
}
try:
  out["site_packages"] = list(site.getsitepackages())
except Exception:
  out["site_packages"] = []
try:
  out["user_site"] = site.getusersitepackages()
except Exception:
  out["user_site"] = None
print(json.dumps(out))
"""
            proc = subprocess.run(
                [reported_python_path, "-c", probe_code],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=60,
            )
            log_f.write(f"[env_size] probe_returncode={proc.returncode}\n")
            if proc.stdout:
                log_f.write(proc.stdout + ("\n" if not proc.stdout.endswith("\n") else ""))
            if proc.stderr:
                log_f.write(proc.stderr + ("\n" if not proc.stderr.endswith("\n") else ""))

            if proc.returncode != 0:
                status = "failure"
                exit_code = 1
                failure_category = "env_size_failed"
            else:
                info = json.loads((proc.stdout or "").strip() or "{}")
                env_prefix = str(info.get("sys_prefix") or "")
                site_paths = list(info.get("site_packages") or [])
                user_site = info.get("user_site")
                if user_site:
                    site_paths.append(str(user_site))
                site_paths = [p for p in site_paths if p]

                observed["env_prefix"] = env_prefix
                if env_prefix:
                    env_prefix_path = Path(env_prefix)
                    if env_prefix_path.exists():
                        env_bytes = _du_bytes(env_prefix_path, warnings)
                        observed["env_prefix_size_MB"] = int(env_bytes / (1024 * 1024))
                    else:
                        warnings.append(f"env_prefix_missing: {env_prefix}")

                site_total = 0
                site_entries = []
                for p in site_paths:
                    pp = Path(p)
                    if not pp.exists():
                        warnings.append(f"site_packages_missing: {p}")
                        continue
                    size_b = _du_bytes(pp, warnings)
                    site_total += size_b
                    site_entries.append({"path": str(pp), "size_bytes": int(size_b)})
                observed["site_packages"] = site_entries
                observed["site_packages_total_bytes"] = int(site_total)

                status = "success"
                exit_code = 0
                failure_category = ""

    results: dict[str, Any] = {
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "stage": stage,
        "task": task,
        "command": command,
        "timeout_sec": 120,
        "framework": "unknown",
        "reported_python_path": reported_python_path,
        "observed": observed,
        "meta": {
            "python": sys.version.split()[0],
            "git_commit": _git_commit(repo_root),
            "env_vars": {
                "SCIMLOPSBENCH_REPORT": os.environ.get("SCIMLOPSBENCH_REPORT", ""),
            },
            "decision_reason": "Measure sizes of sys.prefix and site-packages for the agent-reported python_path environment.",
            "timestamp_utc": _now_utc_iso(),
            "warnings": warnings,
        },
        "assets": _base_assets(),
        "failure_category": failure_category,
        "error_excerpt": _read_last_lines(log_path, max_lines=240),
    }
    _write_json(results_path, results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
