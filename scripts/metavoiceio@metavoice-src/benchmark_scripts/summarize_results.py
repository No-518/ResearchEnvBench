#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGES_ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git_commit(root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), stderr=subprocess.STDOUT, text=True, timeout=5)
            .strip()
        )
    except Exception:  # noqa: BLE001
        return ""


def read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "invalid_json"
        return data, None
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception:  # noqa: BLE001
        return None, "invalid_json"


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    for stage in STAGES_ORDER:
        rpath = root / "build_output" / stage / "results.json"
        lpath = root / "build_output" / stage / "log.txt"
        data, err = read_json(rpath)
        if data is None:
            stages[stage] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err,
                "command": "",
                "results_path": str(rpath),
                "log_path": str(lpath),
            }
            failed.append(stage)
            continue

        status = str(data.get("status", "failure"))
        exit_code = int(data.get("exit_code", 1)) if str(data.get("exit_code", "")).isdigit() else int(data.get("exit_code", 1) or 1)
        failure_category = str(data.get("failure_category", "unknown"))
        command = str(data.get("command", ""))

        stages[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(rpath),
            "log_path": str(lpath),
        }

        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or exit_code == 1:
            failed.append(stage)

    overall_status = "failure" if failed else "success"
    status = "failure" if overall_status == "failure" else "success"
    exit_code = 1 if overall_status == "failure" else 0

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}
    # Pyright metrics
    pyright_data, _ = read_json(root / "build_output" / "pyright" / "results.json")
    if isinstance(pyright_data, dict):
        metrics["pyright"] = {
            "missing_packages_count": pyright_data.get("missing_packages_count"),
            "total_imported_packages_count": pyright_data.get("total_imported_packages_count"),
            "missing_package_ratio": pyright_data.get("missing_package_ratio"),
        }
    # Env size metrics
    env_data, _ = read_json(root / "build_output" / "env_size" / "results.json")
    if isinstance(env_data, dict):
        observed = env_data.get("observed", {}) if isinstance(env_data.get("observed"), dict) else {}
        metrics["env_size"] = {
            "env_prefix_size_MB": observed.get("env_prefix_size_MB"),
            "site_packages_total_bytes": observed.get("site_packages_total_bytes"),
        }
    # Hallucination metrics
    hall_data, _ = read_json(root / "build_output" / "hallucination" / "results.json")
    if isinstance(hall_data, dict):
        h = hall_data.get("hallucinations", {}) if isinstance(hall_data.get("hallucinations"), dict) else {}
        def get_count(k: str) -> Any:
            v = h.get(k)
            return v.get("count") if isinstance(v, dict) else None
        metrics["hallucination"] = {
            "path_hallucinations": get_count("path"),
            "version_hallucinations": get_count("version"),
            "capability_hallucinations": get_count("capability"),
        }

    summary = {
        "status": status,
        "exit_code": exit_code,
        "stage": "summary",
        "task": "validate",
        "command": "benchmark_scripts/summarize_results.py",
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages,
        "metrics": metrics,
        "meta": {"git_commit": git_commit(root), "timestamp_utc": utc_timestamp()},
        "failure_category": "unknown" if exit_code == 0 else "runtime",
    }

    log_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    results_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
