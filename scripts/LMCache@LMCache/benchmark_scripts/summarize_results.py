#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGES_IN_ORDER = [
    "pyright",
    "prepare",
    "cpu",
    "cuda",
    "single_gpu",
    "multi_gpu",
    "env_size",
    "hallucination",
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:  # noqa: BLE001
        return None, f"invalid_json:{e}"


def git_commit(root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        return out
    except Exception:
        return ""


def main(argv: List[str]) -> int:
    _ = argv
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    stages_summary: Dict[str, Dict[str, Any]] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    metrics: Dict[str, Any] = {
        "pyright": {},
        "env_size": {},
        "hallucination": {},
    }

    with log_path.open("w", encoding="utf-8") as log_f:
        log_f.write(f"[summary] start_utc={utc_timestamp()}\n")

        for stage in STAGES_IN_ORDER:
            stage_dir = root / "build_output" / stage
            stage_results_path = stage_dir / "results.json"
            stage_log_path = stage_dir / "log.txt"

            data, err = read_json(stage_results_path)
            if data is None:
                failure_category = "missing_stage_results" if err == "missing" else "invalid_json"
                stage_info = {
                    "status": "failure",
                    "exit_code": 1,
                    "failure_category": failure_category,
                    "command": "",
                    "results_path": str(stage_results_path),
                    "log_path": str(stage_log_path),
                }
                log_f.write(f"[summary] {stage}: {failure_category} ({err})\n")
            else:
                stage_info = {
                    "status": str(data.get("status", "failure")),
                    "exit_code": int(data.get("exit_code", 1)),
                    "failure_category": str(data.get("failure_category", "unknown")),
                    "command": str(data.get("command", "")),
                    "results_path": str(stage_results_path),
                    "log_path": str(stage_log_path),
                }

                # Pull metrics opportunistically.
                if stage == "pyright":
                    for k in (
                        "missing_packages_count",
                        "total_imported_packages_count",
                        "missing_package_ratio",
                    ):
                        if k in data:
                            metrics["pyright"][k] = data.get(k)
                elif stage == "env_size":
                    observed = data.get("observed") if isinstance(data.get("observed"), dict) else {}
                    if "env_prefix_size_MB" in observed:
                        metrics["env_size"]["env_prefix_size_MB"] = observed.get("env_prefix_size_MB")
                    if "site_packages_total_bytes" in observed:
                        metrics["env_size"]["site_packages_total_bytes"] = observed.get("site_packages_total_bytes")
                elif stage == "hallucination":
                    hall = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
                    for k in ("path", "version", "capability"):
                        if isinstance(hall.get(k), dict) and "count" in hall[k]:
                            metrics["hallucination"][f"{k}_count"] = hall[k].get("count")

            stages_summary[stage] = stage_info

            if stage_info["status"] == "skipped":
                skipped_stages.append(stage)
            elif stage_info["status"] == "failure" or stage_info["exit_code"] == 1:
                failed_stages.append(stage)

        overall_status = "failure" if failed_stages else "success"

    payload: Dict[str, Any] = {
        "stage": "summary",
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages_summary,
        "metrics": metrics,
        "meta": {
            "git_commit": git_commit(root),
            "timestamp_utc": utc_timestamp(),
            "cwd": str(root),
        },
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

