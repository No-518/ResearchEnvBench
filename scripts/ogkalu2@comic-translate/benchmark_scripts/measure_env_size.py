#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = Path("/opt/scimlopsbench/report.json")


def _utc_timestamp() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_text(path: Path, content: str) -> None:
    _safe_mkdir(path.parent)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    _safe_mkdir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tail(path: Path, max_lines: int = 240) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        return "".join(lines[-max_lines:])
    except Exception:
        return ""


def resolve_report_path(cli_report_path: str | None) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env_path = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env_path:
        return Path(env_path)
    return DEFAULT_REPORT_PATH


def load_report(report_path: Path) -> Tuple[dict | None, str | None]:
    if not report_path.exists():
        return None, f"Report not found: {report_path}"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, f"Report JSON root is not an object: {report_path}"
        return data, None
    except Exception as e:
        return None, f"Failed to parse report JSON: {report_path}: {e}"


def is_executable(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and os.access(str(path), os.X_OK)
    except Exception:
        return False


@dataclass
class EnvInfo:
    env_prefix: str
    site_packages: List[str]


def get_env_info(python_path: str) -> EnvInfo:
    code = (
        "import json, site, sys\n"
        "data = {\n"
        "  'env_prefix': sys.prefix,\n"
        "  'site_packages': list(dict.fromkeys(site.getsitepackages() + [site.getusersitepackages()])),\n"
        "}\n"
        "print(json.dumps(data))\n"
    )
    out = subprocess.check_output([python_path, "-c", code], text=True)
    data = json.loads(out)
    return EnvInfo(env_prefix=str(data["env_prefix"]), site_packages=[str(p) for p in data["site_packages"]])


def dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        if path.is_symlink():
            # Count symlink itself as 0 to avoid surprising traversals.
            return 0
        if path.is_file():
            return path.stat().st_size
        if not path.exists():
            return 0
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in files:
                fp = Path(root) / name
                try:
                    if fp.is_symlink():
                        continue
                    total += fp.stat().st_size
                except PermissionError:
                    warnings.append(f"PermissionError: {fp}")
                except FileNotFoundError:
                    continue
                except Exception as e:
                    warnings.append(f"{type(e).__name__}: {fp}: {e}")
    except PermissionError:
        warnings.append(f"PermissionError: {path}")
    except Exception as e:
        warnings.append(f"{type(e).__name__}: {path}: {e}")
    return total


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Measure environment footprint from agent report python_path.")
    ap.add_argument("--report-path", default="", help="Override report path.")
    args = ap.parse_args(argv)

    out_dir = REPO_ROOT / "build_output" / "env_size"
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    _safe_mkdir(out_dir)

    header = (
        f"stage=env_size\n"
        f"repo={REPO_ROOT}\n"
        f"out_dir={out_dir}\n"
        f"timestamp_utc={_utc_timestamp()}\n"
        f"runner_python={sys.executable}\n"
    )
    _write_text(log_path, header)

    report_path = resolve_report_path(args.report_path or None)
    report, rep_err = load_report(report_path)

    base_result: Dict[str, Any] = {
        "status": "failure",
        "skip_reason": "not_applicable",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": f"{sys.executable} benchmark_scripts/measure_env_size.py --report-path {report_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "reported_python_path": "",
        "observed": {
            "env_prefix": "",
            "env_prefix_size_MB": 0,
            "site_packages": [],
            "site_packages_total_bytes": 0,
        },
        "meta": {
            "git_commit": "",
            "timestamp_utc": _utc_timestamp(),
            "warnings": [],
            "report_path": str(report_path),
        },
        "failure_category": "env_size_failed",
        "error_excerpt": "",
    }

    try:
        if report is None:
            with log_path.open("a", encoding="utf-8", errors="replace") as f:
                f.write(f"\nERROR: {rep_err}\n")
            base_result["failure_category"] = "env_size_failed"
            base_result["error_excerpt"] = _tail(log_path)
            _write_json(results_path, base_result)
            return 1

        python_path = report.get("python_path")
        if not isinstance(python_path, str) or not python_path.strip():
            with log_path.open("a", encoding="utf-8", errors="replace") as f:
                f.write("\nERROR: report.json missing python_path\n")
            base_result["failure_category"] = "env_size_failed"
            base_result["error_excerpt"] = _tail(log_path)
            _write_json(results_path, base_result)
            return 1

        base_result["reported_python_path"] = python_path
        if not is_executable(Path(python_path)):
            with log_path.open("a", encoding="utf-8", errors="replace") as f:
                f.write(f"\nERROR: python_path not executable: {python_path}\n")
            base_result["failure_category"] = "env_size_failed"
            base_result["error_excerpt"] = _tail(log_path)
            _write_json(results_path, base_result)
            return 1

        env_info = get_env_info(python_path)
        warnings: List[str] = []

        env_prefix_path = Path(env_info.env_prefix)
        env_size = dir_size_bytes(env_prefix_path, warnings)
        env_size_mb = int(round(env_size / (1024 * 1024)))

        site_entries = []
        site_total = 0
        seen: set[str] = set()
        for sp in env_info.site_packages:
            if not sp or sp in seen:
                continue
            seen.add(sp)
            pth = Path(sp)
            if not pth.exists():
                continue
            sz = dir_size_bytes(pth, warnings)
            site_total += sz
            site_entries.append({"path": str(pth), "size_bytes": int(sz)})

        base_result["status"] = "success"
        base_result["exit_code"] = 0
        base_result["observed"]["env_prefix"] = str(env_prefix_path)
        base_result["observed"]["env_prefix_size_MB"] = env_size_mb
        base_result["observed"]["site_packages"] = site_entries
        base_result["observed"]["site_packages_total_bytes"] = int(site_total)
        base_result["meta"]["warnings"] = warnings
        base_result["failure_category"] = "unknown"
        base_result["error_excerpt"] = ""
        _write_json(results_path, base_result)
        return 0
    except Exception as e:
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
            f.write("\nERROR:\n")
            f.write(f"{type(e).__name__}: {e}\n")
            f.write(traceback.format_exc() + "\n")
        base_result["status"] = "failure"
        base_result["exit_code"] = 1
        base_result["failure_category"] = "env_size_failed"
        base_result["error_excerpt"] = _tail(log_path)
        _write_json(results_path, base_result)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
