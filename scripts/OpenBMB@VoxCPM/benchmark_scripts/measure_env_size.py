#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def tail_lines(path: Path, n: int = 220) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def get_git_commit(root: Path) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def load_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"Missing JSON file: {path}"
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON in {path}: {e}"
    except Exception as e:
        return None, f"Failed to read {path}: {e}"


def resolve_report_path(cli: Optional[str]) -> Path:
    if cli:
        p = Path(cli)
        return (p / "report.json") if p.is_dir() else p
    if os.environ.get("SCIMLOPSBENCH_REPORT"):
        p = Path(os.environ["SCIMLOPSBENCH_REPORT"])
        return (p / "report.json") if p.is_dir() else p
    p = Path("/opt/scimlopsbench/report.json")
    return (p / "report.json") if p.is_dir() else p


def dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        if not path.exists():
            warnings.append(f"path_missing: {path}")
            return 0
        if path.is_file():
            return path.stat().st_size
    except Exception as e:
        warnings.append(f"stat_failed: {path}: {e}")
        return 0

    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            fp = Path(root) / name
            try:
                total += fp.stat().st_size
            except PermissionError:
                warnings.append(f"permission_denied: {fp}")
            except FileNotFoundError:
                # Racy deletes during walk.
                continue
            except Exception as e:
                warnings.append(f"stat_failed: {fp}: {e}")
        # Prune dirs we cannot traverse.
        pruned: List[str] = []
        for d in list(dirs):
            dp = Path(root) / d
            try:
                _ = list(dp.iterdir())  # probe permission
            except PermissionError:
                pruned.append(str(dp))
                dirs.remove(d)
            except Exception:
                continue
        for p in pruned:
            warnings.append(f"permission_denied_dir: {p}")
    return total


INFO_SNIPPET = r"""
import json
import site
import sys

out = {
  "python_executable": sys.executable,
  "sys_prefix": sys.prefix,
  "site_getsitepackages": [],
  "site_getusersitepackages": "",
}

try:
  out["site_getsitepackages"] = list(site.getsitepackages())
except Exception:
  out["site_getsitepackages"] = []

try:
  out["site_getusersitepackages"] = site.getusersitepackages()
except Exception:
  out["site_getusersitepackages"] = ""

print(json.dumps(out))
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    root = repo_root()
    out_dir = root / "build_output" / "env_size"
    safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    rpt_path = resolve_report_path(args.report_path)

    status = "failure"
    skip_reason = "not_applicable"
    exit_code = 1
    failure_category = "env_size_failed"
    observed: Dict[str, Any] = {
        "env_prefix": "",
        "env_prefix_size_MB": 0,
        "site_packages": [],
        "site_packages_total_bytes": 0,
    }
    warnings: List[str] = []

    rpt, rpt_err = load_json(rpt_path)
    python_path = None
    if rpt is None or not isinstance(rpt, dict):
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"ERROR: {rpt_err or 'invalid report'}\n")
        python_path = None
    else:
        python_path = rpt.get("python_path")

    if not isinstance(python_path, str) or not python_path:
        with log_path.open("a", encoding="utf-8") as f:
            f.write("ERROR: report.json missing python_path\n")
    elif not (Path(python_path).exists() and os.access(python_path, os.X_OK)):
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"ERROR: python_path is not executable: {python_path}\n")
    else:
        try:
            p = subprocess.run(
                [python_path, "-c", INFO_SNIPPET],
                cwd=str(root),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
                text=True,
            )
            log_path.write_text(p.stdout or "", encoding="utf-8")
            if p.returncode != 0:
                warnings.append(f"python_info_nonzero_exit: {p.returncode}")
            try:
                info = json.loads((p.stdout or "").splitlines()[-1])
            except Exception as e:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(f"\nERROR: failed to parse python info JSON: {e}\n")
            else:
                env_prefix = Path(str(info.get("sys_prefix", "")))
                site_paths: List[Path] = []
                for sp in info.get("site_getsitepackages", []) or []:
                    if sp:
                        site_paths.append(Path(sp))
                usp = info.get("site_getusersitepackages", "")
                if usp:
                    site_paths.append(Path(usp))

                env_prefix_size = dir_size_bytes(env_prefix, warnings)
                site_sizes = []
                total_site = 0
                for sp in site_paths:
                    sz = dir_size_bytes(sp, warnings)
                    site_sizes.append({"path": str(sp), "size_bytes": int(sz)})
                    total_site += int(sz)

                observed = {
                    "env_prefix": str(env_prefix),
                    "env_prefix_size_MB": int(round(env_prefix_size / (1024 * 1024))),
                    "site_packages": site_sizes,
                    "site_packages_total_bytes": int(total_site),
                }
                status = "success"
                exit_code = 0
                failure_category = "unknown"
        except Exception as e:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"\nERROR: env size measurement failed: {e}\n")
            status = "failure"
            exit_code = 1
            failure_category = "env_size_failed"

    payload: Dict[str, Any] = {
        "status": status,
        "skip_reason": skip_reason,
        "exit_code": exit_code,
        "stage": "env_size",
        "task": "measure",
        "command": f"python {Path(__file__).name} --report-path {rpt_path}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "reported_python_path": python_path or "",
        "observed": observed,
        "meta": {
            "git_commit": get_git_commit(root),
            "timestamp_utc": utc_now_iso(),
            "warnings": warnings,
            "env_vars": {k: os.environ.get(k, "") for k in ["SCIMLOPSBENCH_REPORT", "SCIMLOPSBENCH_PYTHON"]},
        },
        "failure_category": failure_category if status == "failure" else "unknown",
        "error_excerpt": tail_lines(log_path),
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
