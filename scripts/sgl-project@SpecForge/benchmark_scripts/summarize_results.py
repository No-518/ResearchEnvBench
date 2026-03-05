#!/usr/bin/env python3
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STAGE_ORDER = ["pyright", "prepare", "cpu", "cuda", "single_gpu", "multi_gpu", "env_size", "hallucination"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def _git_commit(repo_root: Path) -> str:
    if not (repo_root / ".git").exists():
        return ""
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, timeout=10).strip()
    except Exception:
        return ""


def _load_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing_stage_results"
    except Exception:
        return None, "missing_stage_results"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(data, dict):
        return None, "invalid_json"
    return data, None


def main() -> int:
    repo_root = _repo_root()
    out_dir = repo_root / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    command_str = "python benchmark_scripts/summarize_results.py"
    stages_out: Dict[str, Any] = {}
    failed_stages: List[str] = []
    skipped_stages: List[str] = []

    metrics: Dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    with log_path.open("a", encoding="utf-8") as log_f:
        log_f.write(f"[summary] timestamp_utc={_utc_now_iso()}\n")
        for stage in STAGE_ORDER:
            res_path = repo_root / "build_output" / stage / "results.json"
            stage_log_path = repo_root / "build_output" / stage / "log.txt"
            data, err = _load_json(res_path)
            if data is None:
                data = {
                    "status": "failure",
                    "exit_code": 1,
                    "stage": stage,
                    "task": "unknown",
                    "command": "",
                    "failure_category": err,
                }
            status = data.get("status", "failure")
            exit_code = int(data.get("exit_code", 1)) if str(data.get("exit_code", "")).isdigit() else (0 if status == "success" else 1)
            failure_category = data.get("failure_category", "unknown")
            command = data.get("command", "")

            stages_out[stage] = {
                "status": status,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "command": command,
                "results_path": str(res_path),
                "log_path": str(stage_log_path),
            }

            if status == "skipped":
                skipped_stages.append(stage)
            elif status == "failure" or exit_code == 1:
                failed_stages.append(stage)

            # Aggregate metrics if present.
            if stage == "pyright":
                for k in ("missing_packages_count", "total_imported_packages_count", "missing_package_ratio"):
                    if k in data:
                        metrics["pyright"][k] = data.get(k)
            elif stage == "env_size":
                obs = data.get("observed", {}) if isinstance(data.get("observed"), dict) else {}
                if "env_prefix_size_MB" in obs:
                    metrics["env_size"]["env_prefix_size_MB"] = obs.get("env_prefix_size_MB")
                if "site_packages_total_bytes" in obs:
                    metrics["env_size"]["site_packages_total_bytes"] = obs.get("site_packages_total_bytes")
            elif stage == "hallucination":
                hall = data.get("hallucinations", {}) if isinstance(data.get("hallucinations"), dict) else {}
                for htype in ("path", "version", "capability"):
                    entry = hall.get(htype)
                    if isinstance(entry, dict) and "count" in entry:
                        metrics["hallucination"][f"{htype}_count"] = entry.get("count")

        overall_status = "failure" if failed_stages else "success"
        exit_code = 1 if overall_status == "failure" else 0

        # Best-effort summary-level failure category.
        failure_category = "unknown"
        if exit_code == 1:
            for stage in STAGE_ORDER:
                fc = stages_out.get(stage, {}).get("failure_category")
                if fc in ("missing_stage_results", "invalid_json"):
                    failure_category = fc
                    break

        summary = {
            "stage": "summary",
            "status": "success" if exit_code == 0 else "failure",
            "skip_reason": "unknown",
            "exit_code": exit_code,
            "task": "validate",
            "command": command_str,
            "timeout_sec": 120,
            "framework": "unknown",
            "overall_status": overall_status,
            "failed_stages": failed_stages,
            "skipped_stages": skipped_stages,
            "stages": stages_out,
            "metrics": metrics,
            "assets": {
                "dataset": {"path": "", "source": "", "version": "", "sha256": ""},
                "model": {"path": "", "source": "", "version": "", "sha256": ""},
            },
            "meta": {"git_commit": _git_commit(repo_root), "timestamp_utc": _utc_now_iso()},
            "failure_category": failure_category,
        }

        results_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log_f.write(f"[summary] overall_status={overall_status} failed_stages={failed_stages} skipped_stages={skipped_stages}\n")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
