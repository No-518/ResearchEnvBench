#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import site
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPORT_PATH = "/opt/scimlopsbench/report.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def _resolve_report_path(cli_report_path: Optional[str]) -> Path:
    if cli_report_path:
        return Path(cli_report_path)
    env = os.environ.get("SCIMLOPSBENCH_REPORT")
    if env:
        return Path(env)
    return Path(DEFAULT_REPORT_PATH)


def _load_report(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_report"
    except Exception:
        return None, "missing_report"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(data, dict):
        return None, "invalid_json"
    return data, None


def _safe_env_subset(env: Dict[str, str]) -> Dict[str, str]:
    keep_prefixes = ("SCIMLOPSBENCH_", "CUDA_", "HF_", "TRANSFORMERS_", "TORCH", "PYTHON", "WANDB_")
    keep_keys = {"PATH", "HOME", "USER", "SHELL", "PWD", "VIRTUAL_ENV", "CONDA_DEFAULT_ENV", "CONDA_PREFIX"}
    out: Dict[str, str] = {}
    for k, v in env.items():
        if k in keep_keys or any(k.startswith(p) for p in keep_prefixes):
            out[k] = v
    return out


def _git_commit(repo_root: Path) -> str:
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return ""
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, timeout=10).strip()
    except Exception:
        return ""


def _dir_size_bytes(path: Path, warnings: List[str]) -> int:
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False):
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except PermissionError:
                            warnings.append(f"permission_denied_file:{entry.path}")
                    elif entry.is_dir(follow_symlinks=False):
                        total += _dir_size_bytes(Path(entry.path), warnings)
                except PermissionError:
                    warnings.append(f"permission_denied_entry:{entry.path}")
    except FileNotFoundError:
        return 0
    except NotADirectoryError:
        return 0
    except PermissionError:
        warnings.append(f"permission_denied_dir:{str(path)}")
        return 0
    return total


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Measure environment size from agent report python_path")
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "env_size"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    report_path = _resolve_report_path(args.report_path)
    report, report_err = _load_report(report_path)

    base: Dict[str, Any] = {
        "status": "failure",
        "exit_code": 1,
        "stage": "env_size",
        "task": "measure",
        "command": f"python benchmark_scripts/measure_env_size.py --report-path {shlex_quote(str(report_path))}" if args.report_path else "python benchmark_scripts/measure_env_size.py",
        "timeout_sec": 120,
        "reported_python_path": "",
        "framework": "unknown",
        "skip_reason": "not_applicable",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "observed": {
            "env_prefix": "",
            "env_prefix_size_MB": 0,
            "site_packages": [],
            "site_packages_total_bytes": 0,
        },
        "meta": {
            "python": sys.executable,
            "git_commit": _git_commit(repo_root),
            "timestamp_utc": _utc_now_iso(),
            "env_vars": _safe_env_subset(os.environ.copy()),
            "report_path": str(report_path),
            "decision_reason": "Measure environment footprint (sys.prefix and site-packages) using the python_path from the agent report.",
        },
        "failure_category": "env_size_failed",
        "error_excerpt": "",
    }

    def write_and_exit(code: int) -> int:
        base["exit_code"] = code
        base["status"] = "success" if code == 0 else "failure"
        try:
            base["error_excerpt"] = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-220:])
        except Exception:
            base["error_excerpt"] = ""
        results_path.write_text(json.dumps(base, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return code

    with log_path.open("a", encoding="utf-8") as log_f:
        log_f.write(f"[env_size] timestamp_utc={_utc_now_iso()}\n")
        log_f.write(f"[env_size] report_path={report_path}\n")

        if report is None:
            log_f.write(f"[env_size] ERROR: report_error={report_err}\n")
            base["failure_category"] = "env_size_failed"
            return write_and_exit(1)

        python_path = report.get("python_path")
        base["reported_python_path"] = str(python_path or "")
        if not isinstance(python_path, str) or not python_path.strip():
            log_f.write("[env_size] ERROR: python_path missing in report\n")
            base["failure_category"] = "env_size_failed"
            return write_and_exit(1)

        python_exe = Path(python_path)
        if not (python_exe.exists() and python_exe.is_file() and os.access(str(python_exe), os.X_OK)):
            log_f.write(f"[env_size] ERROR: python_path not executable: {python_exe}\n")
            base["failure_category"] = "env_size_failed"
            return write_and_exit(1)

        # Ask the reported python to report sys.prefix + site-packages paths.
        probe_code = (
            "import json, sys, site\n"
            "out={'sys_prefix':sys.prefix,'site_packages':[],'user_site':None}\n"
            "try:\n"
            "  out['site_packages']=site.getsitepackages()\n"
            "except Exception as e:\n"
            "  out['site_packages_error']=str(e)\n"
            "try:\n"
            "  out['user_site']=site.getusersitepackages()\n"
            "except Exception as e:\n"
            "  out['user_site_error']=str(e)\n"
            "print(json.dumps(out))\n"
        )
        cmd = [str(python_exe), "-c", probe_code]
        log_f.write(f"[env_size] probe_cmd={shlex_join(cmd)}\n")
        log_f.flush()
        try:
            proc = subprocess.run(cmd, cwd=str(repo_root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        except subprocess.TimeoutExpired:
            log_f.write("[env_size] ERROR: probe timeout\n")
            base["failure_category"] = "env_size_failed"
            return write_and_exit(1)

        if proc.stderr:
            log_f.write("[env_size][stderr]\n")
            log_f.write(proc.stderr)
            log_f.write("\n")
        log_f.write("[env_size][stdout]\n")
        log_f.write(proc.stdout)
        log_f.write("\n")
        log_f.flush()

    try:
        probe = json.loads(proc.stdout.strip() or "{}")
    except Exception as e:
        base["failure_category"] = "env_size_failed"
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write(f"[env_size] ERROR: invalid probe JSON: {e}\n")
        return write_and_exit(1)

    env_prefix = Path(str(probe.get("sys_prefix", "")))
    site_paths: List[Path] = []
    for p in probe.get("site_packages", []) if isinstance(probe.get("site_packages"), list) else []:
        site_paths.append(Path(str(p)))
    user_site = probe.get("user_site")
    if isinstance(user_site, str) and user_site.strip():
        site_paths.append(Path(user_site))

    warnings: List[str] = []
    env_prefix_size = _dir_size_bytes(env_prefix, warnings) if env_prefix.exists() else 0
    site_entries = []
    site_total = 0
    for sp in site_paths:
        size = _dir_size_bytes(sp, warnings) if sp.exists() else 0
        site_entries.append({"path": str(sp), "size_bytes": int(size)})
        site_total += int(size)

    base["observed"]["env_prefix"] = str(env_prefix)
    base["observed"]["env_prefix_size_MB"] = int(env_prefix_size / (1024 * 1024))
    base["observed"]["site_packages"] = site_entries
    base["observed"]["site_packages_total_bytes"] = int(site_total)
    base["meta"]["warnings"] = warnings

    # Success as long as we could observe sys.prefix; sizes may be 0 on permission issues.
    base["failure_category"] = "unknown"
    return write_and_exit(0)


def shlex_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


def shlex_join(cmd: List[str]) -> str:
    import shlex

    return " ".join(shlex.quote(c) for c in cmd)


if __name__ == "__main__":
    raise SystemExit(main())
