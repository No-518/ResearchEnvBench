#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bench_utils import REPO_ROOT, ensure_dir, get_git_commit, read_json, tail_lines, utc_timestamp, write_json


STAGE_ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def load_stage(stage: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = REPO_ROOT / "build_output" / stage / "results.json"
    if not path.exists():
        return None, "missing_stage_results"
    try:
        data = read_json(path)
        return data if isinstance(data, dict) else None, "invalid_json"
    except json.JSONDecodeError:
        return None, "invalid_json"
    except Exception:
        return None, "invalid_json"


def stage_paths(stage: str) -> Dict[str, str]:
    return {
        "results_path": str(REPO_ROOT / "build_output" / stage / "results.json"),
        "log_path": str(REPO_ROOT / "build_output" / stage / "log.txt"),
    }


def main() -> int:
    out_dir = REPO_ROOT / "build_output" / "summary"
    ensure_dir(out_dir)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages_summary: Dict[str, Any] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    log_lines: List[str] = []
    log_lines.append(f"timestamp_utc={utc_timestamp()}")

    for stage in STAGE_ORDER:
        data, err = load_stage(stage)
        if data is None:
            stage_entry = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err,
                "command": "",
                **stage_paths(stage),
            }
        else:
            stage_entry = {
                "status": data.get("status", "failure"),
                "exit_code": int(data.get("exit_code", 1) or 1),
                "failure_category": data.get("failure_category", ""),
                "command": data.get("command", ""),
                **stage_paths(stage),
            }

        stages_summary[stage] = stage_entry

        status = str(stage_entry["status"])
        exit_code = int(stage_entry["exit_code"])

        if status == "skipped":
            skipped_stages.append(stage)
        elif status == "failure" or exit_code == 1:
            failed_stages.append(stage)

        # Pull metrics
        if stage == "pyright" and data:
            m = data.get("metrics") or {}
            if isinstance(m, dict):
                for k in ["missing_packages_count", "total_imported_packages_count", "missing_package_ratio"]:
                    if k in m:
                        metrics["pyright"][k] = m.get(k)
        if stage == "env_size" and data:
            obs = data.get("observed") or {}
            if isinstance(obs, dict):
                if "env_prefix_size_MB" in obs:
                    metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
                if "site_packages_total_bytes" in obs:
                    metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")
        if stage == "hallucination" and data:
            h = data.get("hallucinations") or {}
            if isinstance(h, dict):
                for key in ["path", "version", "capability"]:
                    if isinstance(h.get(key), dict):
                        metrics["hallucination"][f"{key}_count"] = h[key].get("count", 0)

        log_lines.append(f"{stage}: status={stage_entry['status']} exit_code={stage_entry['exit_code']}")

    overall_status = "success"
    if failed_stages:
        overall_status = "failure"

    status = "success" if overall_status == "success" else "failure"
    exit_code = 0 if status == "success" else 1

    summary = {
        "stage": "summary",
        "status": status,
        "skip_reason": "not_applicable",
        "exit_code": exit_code,
        "task": "validate",
        "command": f"python {Path(__file__).name}",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages_summary,
        "metrics": metrics,
        "meta": {"git_commit": get_git_commit(REPO_ROOT), "timestamp_utc": utc_timestamp()},
        "failure_category": "not_applicable" if status == "success" else "unknown",
        "error_excerpt": tail_lines(log_path),
    }

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    write_json(results_path, summary)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
