#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGE_ORDER = [
    "pyright",
    "prepare",
    "cpu",
    "cuda",
    "single_gpu",
    "multi_gpu",
    "env_size",
    "hallucination",
]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _git_commit(repo: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo), stderr=subprocess.DEVNULL, text=True)
            .strip()
        )
    except Exception:
        return ""


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, "missing_stage_results"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except Exception:
        return None, "invalid_json"


def main(argv: list[str]) -> int:
    _ = argv
    repo = _repo_root()
    out_dir = repo / "build_output" / "summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "log.txt"
    results_path = out_dir / "results.json"

    log_path.write_text("", encoding="utf-8")

    stages_summary: dict[str, Any] = {}
    failed_stages: list[str] = []
    skipped_stages: list[str] = []

    metrics: dict[str, Any] = {"pyright": {}, "env_size": {}, "hallucination": {}}

    for stage in STAGE_ORDER:
        stage_dir = repo / "build_output" / stage
        res_path = stage_dir / "results.json"
        stage_data, err = _load_json(res_path)
        if err:
            stage_entry = {
                "status": "failure",
                "exit_code": 1,
                "failure_category": err,
                "command": "",
                "results_path": str(res_path),
                "log_path": str(stage_dir / "log.txt"),
            }
        else:
            stage_entry = {
                "status": stage_data.get("status", "failure"),
                "exit_code": int(stage_data.get("exit_code", 1) or 1),
                "failure_category": stage_data.get("failure_category", ""),
                "command": stage_data.get("command", ""),
                "results_path": str(res_path),
                "log_path": str(stage_dir / "log.txt"),
            }

        stages_summary[stage] = stage_entry

        if stage_entry["status"] == "skipped":
            skipped_stages.append(stage)
        elif stage_entry["status"] == "failure" or stage_entry["exit_code"] == 1:
            failed_stages.append(stage)

        # Aggregated metrics.
        if stage == "pyright" and stage_data:
            m = stage_data.get("metrics") or {}
            metrics["pyright"] = {
                "missing_packages_count": m.get("missing_packages_count"),
                "total_imported_packages_count": m.get("total_imported_packages_count"),
                "missing_package_ratio": m.get("missing_package_ratio"),
            }
        if stage == "env_size" and stage_data:
            obs = stage_data.get("observed") or {}
            metrics["env_size"] = {
                "env_prefix_size_MB": obs.get("env_prefix_size_MB"),
                "site_packages_total_bytes": obs.get("site_packages_total_bytes"),
            }
        if stage == "hallucination" and stage_data:
            h = stage_data.get("hallucinations") or {}
            metrics["hallucination"] = {
                "path_count": (h.get("path") or {}).get("count"),
                "version_count": (h.get("version") or {}).get("count"),
                "capability_count": (h.get("capability") or {}).get("count"),
            }

    overall_status = "failure" if failed_stages else "success"
    summary = {
        "stage": "summary",
        "overall_status": overall_status,
        "failed_stages": failed_stages,
        "skipped_stages": skipped_stages,
        "stages": stages_summary,
        "metrics": metrics,
        "meta": {"git_commit": _git_commit(repo), "timestamp_utc": _utc_timestamp()},
    }

    results_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_path.write_text(json.dumps({"failed_stages": failed_stages, "skipped_stages": skipped_stages}) + "\n", encoding="utf-8")

    return 1 if overall_status == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

