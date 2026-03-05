#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
STAGE = "summary"
ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def git_commit() -> str:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:
        return ""


def load_json(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception:
        return None, "invalid_json"


def main() -> int:
    out_dir = REPO_ROOT / "build_output" / STAGE
    safe_mkdir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages_out: Dict[str, Any] = {}
    failed: List[str] = []
    skipped: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    for stage in ORDER:
        stage_dir = REPO_ROOT / "build_output" / stage
        stage_results_path = stage_dir / "results.json"
        stage_log_path = stage_dir / "log.txt"
        data, err = load_json(stage_results_path)

        if data is None or not isinstance(data, dict):
            stages_out[stage] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err,
                "command": "",
                "results_path": str(stage_results_path),
                "log_path": str(stage_log_path),
            }
            failed.append(stage)
            continue

        status = data.get("status")
        exit_code = int(data.get("exit_code", 1) or 0)
        failure_category = data.get("failure_category", "unknown")
        command = data.get("command", "")

        stages_out[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(stage_results_path),
            "log_path": str(stage_log_path),
        }

        if status == "skipped":
            skipped.append(stage)
        elif status == "failure" or exit_code == 1:
            failed.append(stage)

        # Pull metrics when present
        if stage == "pyright":
            metrics["pyright"]["missing_packages_count"] = data.get("missing_packages_count")
            metrics["pyright"]["total_imported_packages_count"] = data.get("total_imported_packages_count")
            metrics["pyright"]["missing_package_ratio"] = data.get("missing_package_ratio")
        elif stage == "env_size":
            obs = data.get("observed") if isinstance(data.get("observed"), dict) else {}
            metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
            metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")
        elif stage == "hallucination":
            hall = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
            for k in ("path", "version", "capability"):
                if isinstance(hall.get(k), dict):
                    metrics["hallucination"][f"{k}_count"] = hall[k].get("count")

    overall_status = "failure" if any(s in failed for s in ORDER) else "success"
    exit_code = 1 if overall_status == "failure" else 0
    status = "failure" if overall_status == "failure" else "success"

    summary = {
        "stage": STAGE,
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "task": "summarize",
        "command": "summarize_results.py",
        "timeout_sec": 0,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "overall_status": overall_status,
        "failed_stages": failed,
        "skipped_stages": skipped,
        "stages": stages_out,
        "metrics": metrics,
        "meta": {"git_commit": git_commit(), "timestamp_utc": utc_now()},
        "failure_category": "not_applicable" if exit_code == 0 else "unknown",
        "error_excerpt": "",
    }

    log_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    results_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
