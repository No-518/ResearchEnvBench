#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGES_ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception:
        return None, "invalid_json"


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"
    log_path.write_text("", encoding="utf-8")

    stages: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    for stage in STAGES_ORDER:
        stage_dir = root / "build_output" / stage
        res_path = stage_dir / "results.json"
        stage_log = stage_dir / "log.txt"

        data, err = read_json(res_path)
        if data is None:
            status = "failure"
            exit_code = 1
            failure_category = err or "invalid_json"
            command = ""
            entry = {
                "status": status,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "command": command,
                "results_path": str(res_path),
                "log_path": str(stage_log),
            }
            stages[stage] = entry
            failed.append(stage)
            with log_path.open("a", encoding="utf-8") as log_fp:
                log_fp.write(f"[summary] {stage}: {failure_category} ({res_path})\n")
            continue

        status = str(data.get("status") or "failure")
        try:
            exit_code = int(data.get("exit_code"))
        except Exception:
            exit_code = 1
        failure_category = str(data.get("failure_category") or "unknown")
        command = str(data.get("command") or "")

        stages[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(res_path),
            "log_path": str(stage_log),
        }

        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or exit_code == 1:
            failed.append(stage)

    overall_status = "failure" if failed else "success"
    status = overall_status
    exit_code = 1 if overall_status == "failure" else 0

    metrics: Dict[str, Any] = {}

    # Pyright metrics.
    pyright_data, _ = read_json(root / "build_output" / "pyright" / "results.json")
    if pyright_data:
        m = pyright_data.get("metrics") or {}
        metrics["pyright"] = {
            "missing_packages_count": m.get("missing_packages_count"),
            "total_imported_packages_count": m.get("total_imported_packages_count"),
            "missing_package_ratio": m.get("missing_package_ratio"),
        }

    # Env size metrics.
    env_data, _ = read_json(root / "build_output" / "env_size" / "results.json")
    if env_data:
        obs = env_data.get("observed") or {}
        metrics["env_size"] = {
            "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
            "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
        }

    # Hallucination counts.
    hall_data, _ = read_json(root / "build_output" / "hallucination" / "results.json")
    if hall_data:
        h = hall_data.get("hallucinations") or {}
        metrics["hallucination"] = {
            "path_hallucinations": (h.get("path") or {}).get("count"),
            "version_hallucinations": (h.get("version") or {}).get("count"),
            "capability_hallucinations": (h.get("capability") or {}).get("count"),
        }

    git_commit = ""
    try:
        git_commit = (
            subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True)
            .strip()
        )
    except Exception:
        git_commit = ""

    summary = {
        "stage": "summary",
        "status": status,
        "exit_code": exit_code,
        "task": "summarize",
        "skip_reason": "unknown",
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages,
        "metrics": metrics,
        "meta": {"git_commit": git_commit, "timestamp_utc": utc_ts()},
        "failure_category": "none" if exit_code == 0 else "unknown",
    }

    results_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
