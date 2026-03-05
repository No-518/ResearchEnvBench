#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    return Path(__file__).resolve().parent.parent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception:
        return None, "missing_stage_results"
    try:
        data = json.loads(raw)
    except Exception:
        return None, "invalid_json"
    if not isinstance(data, dict):
        return None, "invalid_json"
    return data, None


def main() -> int:
    root = repo_root()
    out_dir = root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    def log(msg: str) -> None:
        log_path.write_text(log_path.read_text(encoding="utf-8", errors="replace") + msg + "\n" if log_path.exists() else msg + "\n", encoding="utf-8")
        print(msg)

    # Fresh log
    log_path.write_text("", encoding="utf-8")
    log(f"[summary] start_utc={utc_now_iso()}")

    stages: dict[str, Any] = {}
    failed_stages: list[str] = []
    skipped_stages: list[str] = []

    metrics: dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    for stage in STAGES_IN_ORDER:
        stage_dir = root / "build_output" / stage
        results_file = stage_dir / "results.json"
        log_file = stage_dir / "log.txt"
        data, err = safe_load_json(results_file)
        if err is not None or data is None:
            stages[stage] = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err,
                "command": "",
                "results_path": str(results_file),
                "log_path": str(log_file),
            }
            failed_stages.append(stage)
            continue

        status = str(data.get("status", ""))
        exit_code = data.get("exit_code", 0)
        failure_category = str(data.get("failure_category", "unknown"))
        command = str(data.get("command", ""))

        stages[stage] = {
            "status": status,
            "exit_code": exit_code,
            "failure_category": failure_category,
            "command": command,
            "results_path": str(results_file),
            "log_path": str(log_file),
        }

        if status == "skipped":
            skipped_stages.append(stage)
        elif status == "failure" or int(exit_code) == 1:
            failed_stages.append(stage)

        # Metrics aggregation
        if stage == "pyright":
            for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
                if k in data:
                    metrics["pyright"][k] = data.get(k)
        if stage == "env_size":
            obs = data.get("observed") if isinstance(data.get("observed"), dict) else {}
            if isinstance(obs, dict):
                if "env_prefix_size_MB" in obs:
                    metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
                if "site_packages_total_bytes" in obs:
                    metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")
        if stage == "hallucination":
            hall = data.get("hallucinations") if isinstance(data.get("hallucinations"), dict) else {}
            if isinstance(hall, dict):
                for k in ("path", "version", "capability"):
                    v = hall.get(k)
                    if isinstance(v, dict) and "count" in v:
                        metrics["hallucination"][f"{k}_count"] = v.get("count")

    overall_status = "failure" if failed_stages else "success"

    payload = {
        "status": "success" if overall_status == "success" else "failure",
        "skip_reason": "not_applicable",
        "exit_code": 0 if overall_status == "success" else 1,
        "stage": "summary",
        "task": "summarize",
        "command": f"{sys.executable} benchmark_scripts/summarize_results.py",
        "timeout_sec": 120,
        "framework": "unknown",
        "assets": {
            "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
            "model": {"path": "", "source": "", "version": "", "sha256": ""},
        },
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages,
        "metrics": metrics,
        "meta": {
            "git_commit": "",
            "timestamp_utc": utc_now_iso(),
        },
        "failure_category": "unknown" if overall_status == "success" else "unknown",
        "error_excerpt": "",
    }

    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"[summary] overall_status={overall_status}")
    log(f"[summary] failed_stages={failed_stages}")
    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
